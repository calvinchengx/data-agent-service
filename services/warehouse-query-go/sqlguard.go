// The SQL guard, again — same rules, different language, and the reason this
// file is long.
//
// The Python executor parses with sqlglot and decides on a tree. Go has no
// T-SQL parser of comparable coverage, so this is a bounded RECOGNISER: it
// tokenises the statement (aware of strings, bracket-quoted identifiers and
// both comment forms), then walks the token stream to answer exactly the
// questions the policy asks — is this one statement, is it a SELECT, does it
// name a forbidden construct, which tables does it read, which columns.
//
// A recogniser is weaker than a parser, so it is written to FAIL CLOSED: any
// construct it does not understand is refused rather than passed through, and
// an unqualified column with several tables in scope is attributed to all of
// them. The conformance suite runs the same corpus against both executors, so
// a disagreement is a test failure rather than a surprise in production.
package main

import (
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

type Policy struct {
	Dialect        string
	AllowedSchemas []string
	MaxRows        int
	MaxLength      int
	Database       string
}

type Verdict struct {
	SQL      string
	Tables   []string
	Columns  []string
	RowLimit int
}

type DeniedError struct{ msg string }

func (e *DeniedError) Error() string { return e.msg }

func denied(format string, args ...any) error {
	return &DeniedError{msg: fmt.Sprintf(format, args...)}
}

// Constructs that must never appear, whatever surrounds them.
var forbiddenKeywords = map[string]string{
	"INSERT": "read-only", "UPDATE": "read-only", "DELETE": "read-only",
	"MERGE": "read-only", "DROP": "read-only", "CREATE": "read-only",
	"ALTER": "read-only", "TRUNCATE": "read-only", "GRANT": "read-only",
	"REVOKE": "read-only", "EXEC": "read-only", "EXECUTE": "read-only",
	"BACKUP": "read-only", "RESTORE": "read-only", "SHUTDOWN": "read-only",
	"USE": "read-only", "SET": "read-only", "DECLARE": "read-only",
	"BEGIN": "read-only", "COMMIT": "read-only", "ROLLBACK": "read-only",
}

var deniedCalls = map[string]bool{
	"OPENROWSET": true, "OPENQUERY": true, "OPENDATASOURCE": true, "OPENXML": true,
	"SP_EXECUTESQL": true, "XP_CMDSHELL": true, "SP_OACREATE": true, "SP_SEND_DBMAIL": true,
}

var deniedPrefixes = []string{"XP_", "SP_", "FN_TRACE"}

type tokenKind int

const (
	tokWord tokenKind = iota
	tokString
	tokNumber
	tokPunct
)

type token struct {
	kind tokenKind
	text string // as written
	up   string // upper-cased, for words
	// quoted marks an identifier written as [name] or "name". A quoted
	// identifier is a NAME, never a keyword: a column legitimately called
	// [drop] must not be read as the DROP statement. Without this the guard
	// refuses valid queries, which is a quieter failure than allowing invalid
	// ones but still a wrong answer.
	quoted bool
}

// tokenise splits a statement into words, strings, numbers and punctuation,
// dropping comments. It reports an error for anything unterminated, because an
// unterminated string is how a statement smuggles a second one.
func tokenise(sql string) ([]token, error) {
	var out []token
	runes := []rune(sql)
	for i := 0; i < len(runes); {
		ch := runes[i]
		switch {
		case ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r':
			i++
		case ch == '-' && i+1 < len(runes) && runes[i+1] == '-':
			for i < len(runes) && runes[i] != '\n' {
				i++
			}
		case ch == '/' && i+1 < len(runes) && runes[i+1] == '*':
			depth, j := 1, i+2
			for j < len(runes) && depth > 0 {
				if runes[j] == '/' && j+1 < len(runes) && runes[j+1] == '*' {
					depth, j = depth+1, j+2
				} else if runes[j] == '*' && j+1 < len(runes) && runes[j+1] == '/' {
					depth, j = depth-1, j+2
				} else {
					j++
				}
			}
			if depth != 0 {
				return nil, denied("could not parse as %s: unterminated comment", "tsql")
			}
			i = j
		case ch == '\'':
			j := i + 1
			for j < len(runes) {
				if runes[j] == '\'' {
					if j+1 < len(runes) && runes[j+1] == '\'' {
						j += 2
						continue
					}
					break
				}
				j++
			}
			if j >= len(runes) {
				return nil, denied("could not parse as tsql: unterminated string")
			}
			out = append(out, token{kind: tokString, text: string(runes[i : j+1])})
			i = j + 1
		case ch == '[' || ch == '"':
			closer := ']'
			if ch == '"' {
				closer = '"'
			}
			j := i + 1
			for j < len(runes) && runes[j] != closer {
				j++
			}
			if j >= len(runes) {
				return nil, denied("could not parse as tsql: unterminated identifier")
			}
			word := string(runes[i+1 : j])
			out = append(out, token{
				kind: tokWord, text: word, up: strings.ToUpper(word), quoted: true,
			})
			i = j + 1
		case isWordRune(ch):
			j := i
			for j < len(runes) && isWordRune(runes[j]) {
				j++
			}
			word := string(runes[i:j])
			kind := tokWord
			if word[0] >= '0' && word[0] <= '9' {
				kind = tokNumber
			}
			out = append(out, token{kind: kind, text: word, up: strings.ToUpper(word)})
			i = j
		default:
			out = append(out, token{kind: tokPunct, text: string(ch), up: string(ch)})
			i++
		}
	}
	return out, nil
}

func isWordRune(r rune) bool {
	return r == '_' || r == '#' || r == '@' || r == '$' ||
		(r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9')
}

// Guard applies the policy. The order matches the Python implementation so the
// same statement earns the same refusal in both.
func Guard(sql string, p Policy) (*Verdict, error) {
	if strings.TrimSpace(sql) == "" {
		return nil, denied("empty statement")
	}
	if p.MaxLength > 0 && len(sql) > p.MaxLength {
		return nil, denied("statement is longer than %d characters", p.MaxLength)
	}
	toks, err := tokenise(sql)
	if err != nil {
		return nil, err
	}
	if len(toks) == 0 {
		return nil, denied("empty statement")
	}

	// 1. one statement. A trailing semicolon is punctuation, not a second one.
	for i, t := range toks {
		if t.kind == tokPunct && t.text == ";" && hasMore(toks, i) {
			return nil, denied("exactly one statement is allowed; got 2")
		}
	}

	// 2. the root must be a SELECT (a CTE counts).
	first := toks[0].up
	if first != "SELECT" && first != "WITH" && first != "(" {
		if reason, bad := forbiddenKeywords[first]; bad {
			return nil, denied("only SELECT is allowed; this endpoint is %s (got %s)", reason, first)
		}
		return nil, denied("only SELECT is allowed; this endpoint is read-only (got %s)", first)
	}

	// 3. no forbidden construct anywhere, and no SELECT … INTO.
	for i, t := range toks {
		if t.kind != tokWord || t.quoted {
			// A quoted identifier is a name. Unquoted CREATE/DROP as a column
			// alias is still refused: telling those apart would be guessing,
			// and quoting is how a caller says which one they meant.
			continue
		}
		if _, bad := forbiddenKeywords[t.up]; bad {
			return nil, denied("%s is not allowed; this endpoint is read-only", t.up)
		}
		if t.up == "INTO" && !precededBy(toks, i, "INSERT") {
			return nil, denied("SELECT … INTO writes a table; this endpoint is read-only")
		}
	}

	// 4. denied callables: a word immediately followed by "(".
	for i, t := range toks {
		if t.kind != tokWord || !nextIs(toks, i, "(") {
			continue
		}
		if deniedCalls[t.up] {
			return nil, denied("function %s is not allowed", strings.ToLower(t.text))
		}
		for _, prefix := range deniedPrefixes {
			if strings.HasPrefix(t.up, prefix) {
				return nil, denied("function %s is not allowed", strings.ToLower(t.text))
			}
		}
	}

	// 5. tables, aliases and CTE names.
	ctes := cteNames(toks)
	tables, aliases, err := tableRefs(toks, ctes, p)
	if err != nil {
		return nil, err
	}
	if len(tables) == 0 {
		// A recogniser cannot tell every typo from a legitimate table-less
		// SELECT, but it can tell THIS one: a qualified name that no clause
		// introduced is a mangled clause, not a constant expression. Saying so
		// matters because the two refusals mean different things to the
		// caller, and the conformance suite holds both implementations to the
		// same wording.
		if !hasKeyword(toks, "FROM") && !hasKeyword(toks, "JOIN") {
			if name := danglingQualifiedName(toks); name != "" {
				return nil, denied("could not parse as %s: expected FROM before %s",
					p.Dialect, name)
			}
		}
		return nil, denied("the query reads no table")
	}

	// 6. the row ceiling, applied by rewriting rather than by trusting.
	limited, limit := applyTop(sql, toks, p)
	return &Verdict{SQL: limited, Tables: tables, Columns: columnsRead(toks, tables, aliases),
		RowLimit: limit}, nil
}

func hasKeyword(toks []token, word string) bool {
	for _, t := range toks {
		if t.kind == tokWord && !t.quoted && t.up == word {
			return true
		}
	}
	return false
}

// danglingQualifiedName reports a `schema.table` that no clause introduced.
func danglingQualifiedName(toks []token) string {
	for i, t := range toks {
		if t.kind != tokWord || sqlKeywords[t.up] {
			continue
		}
		if i+2 < len(toks) && toks[i+1].kind == tokPunct && toks[i+1].text == "." &&
			toks[i+2].kind == tokWord {
			return t.text + "." + toks[i+2].text
		}
	}
	return ""
}

func hasMore(toks []token, at int) bool {
	for _, t := range toks[at+1:] {
		if t.kind != tokPunct || t.text != ";" {
			return true
		}
	}
	return false
}

func nextIs(toks []token, at int, text string) bool {
	return at+1 < len(toks) && toks[at+1].text == text
}

func precededBy(toks []token, at int, word string) bool {
	return at > 0 && toks[at-1].up == word
}

func cteNames(toks []token) map[string]bool {
	names := map[string]bool{}
	for i, t := range toks {
		if t.up == "WITH" && i+1 < len(toks) && toks[i+1].kind == tokWord {
			names[toks[i+1].up] = true
		}
		// `, name AS (` continues a CTE list
		if t.kind == tokPunct && t.text == "," && i+2 < len(toks) &&
			toks[i+1].kind == tokWord && toks[i+2].up == "AS" &&
			i+3 < len(toks) && toks[i+3].text == "(" {
			names[toks[i+1].up] = true
		}
	}
	return names
}

// tableRefs collects what follows FROM and JOIN, enforcing the schema rules,
// and records each reference's alias so columns can be attributed.
func tableRefs(toks []token, ctes map[string]bool, p Policy) ([]string, map[string]string, error) {
	allowed := map[string]bool{}
	for _, s := range p.AllowedSchemas {
		allowed[strings.ToUpper(s)] = true
	}
	seen := map[string]bool{}
	aliases := map[string]string{}
	var tables []string

	for i, t := range toks {
		if t.kind != tokWord || (t.up != "FROM" && t.up != "JOIN") {
			continue
		}
		// `FROM a, b, c` names three tables and only the first follows the
		// FROM keyword. Until this loop existed the rest were invisible: the
		// schema allow-list, the cross-database rule and the tables recorded
		// for the access rules and the audit all saw the first one only.
		at := i + 1
		for {
			var err error
			at, err = scanTableRef(toks, at, ctes, p, allowed, seen, &tables, aliases)
			if err != nil {
				return nil, nil, err
			}
			if at < len(toks) && toks[at].kind == tokPunct && toks[at].text == "," {
				at++
				continue
			}
			break
		}
	}
	sort.Strings(tables)
	return tables, aliases, nil
}

// scanTableRef reads one table reference and returns where it stopped.
func scanTableRef(
	toks []token, at int, ctes map[string]bool, p Policy, allowed map[string]bool,
	seen map[string]bool, tables *[]string, aliases map[string]string,
) (int, error) {
	{
		parts, next := qualifiedName(toks, at)
		if len(parts) == 0 {
			return next, nil // a derived table: `FROM (SELECT …)`, its own tokens are scanned
		}
		if len(parts) == 1 && ctes[strings.ToUpper(parts[0])] {
			alias, after := aliasAt(toks, next)
			_ = alias
			return after, nil
		}
		// A TABLE FUNCTION is not a table. An engine that reads a file as a
		// relation -- DuckDB's read_csv_auto, read_parquet, glob -- turns a
		// SELECT into arbitrary file access, and a schema-qualified call
		// (`dbo.read_csv_auto('/etc/passwd')`) satisfies every other rule
		// here. The tell is a `(` where an alias or a comma should be.
		if next < len(toks) && toks[next].kind == tokPunct && toks[next].text == "(" {
			return 0, denied(
				"%s is a table function, not a table; only tables in %s may be read",
				strings.Join(parts, "."), strings.Join(p.AllowedSchemas, ", "))
		}
		var catalog, schema, name string
		switch len(parts) {
		case 1:
			name = parts[0]
		case 2:
			schema, name = parts[0], parts[1]
		case 3:
			catalog, schema, name = parts[0], parts[1], parts[2]
		default:
			return 0, denied("four-part name %s is not allowed", strings.Join(parts, "."))
		}
		if catalog != "" {
			if p.Database == "" || !strings.EqualFold(catalog, p.Database) {
				return 0, denied("cross-database reference %s.%s.%s is not allowed",
					catalog, schema, name)
			}
		}
		if schema == "" {
			return 0, denied("table %s must be schema-qualified (e.g. %s.%s)",
				name, strings.ToLower(firstSchema(p)), name)
		}
		if !allowed[strings.ToUpper(schema)] {
			return 0, denied("schema %s is not queryable; allowed: %s",
				schema, strings.Join(p.AllowedSchemas, ", "))
		}
		qualified := strings.ToLower(schema + "." + name)
		if !seen[qualified] {
			seen[qualified] = true
			*tables = append(*tables, qualified)
		}
		aliases[strings.ToUpper(name)] = qualified
		alias, after := aliasAt(toks, next)
		if alias != "" {
			aliases[strings.ToUpper(alias)] = qualified
		}
		return after, nil
	}
}

func firstSchema(p Policy) string {
	if len(p.AllowedSchemas) == 0 {
		return "dbo"
	}
	return p.AllowedSchemas[0]
}

// qualifiedName reads `a`, `a.b` or `a.b.c` starting at `at`.
func qualifiedName(toks []token, at int) ([]string, int) {
	if at >= len(toks) || toks[at].kind != tokWord {
		return nil, at
	}
	parts := []string{toks[at].text}
	i := at + 1
	for i+1 < len(toks) && toks[i].kind == tokPunct && toks[i].text == "." &&
		toks[i+1].kind == tokWord {
		parts = append(parts, toks[i+1].text)
		i += 2
	}
	return parts, i
}

var notAnAlias = map[string]bool{
	"ON": true, "WHERE": true, "GROUP": true, "ORDER": true, "HAVING": true, "JOIN": true,
	"INNER": true, "LEFT": true, "RIGHT": true, "FULL": true, "CROSS": true, "OUTER": true,
	"UNION": true, "EXCEPT": true, "INTERSECT": true, "AS": true, "WITH": true, "OPTION": true,
}

func aliasAfter(toks []token, at int) string {
	alias, _ := aliasAt(toks, at)
	return alias
}

// aliasAt is aliasAfter, plus where it stopped. The caller needs that to see
// whether a comma follows -- `FROM a, b` lists a second table with no FROM or
// JOIN in front of it, and a scan that only looks after those keywords does
// not see it at all.
func aliasAt(toks []token, at int) (string, int) {
	if at < len(toks) && toks[at].up == "AS" {
		at++
	}
	if at < len(toks) && toks[at].kind == tokWord && !notAnAlias[toks[at].up] {
		return toks[at].text, at + 1
	}
	return "", at
}

var sqlKeywords = map[string]bool{
	"SELECT": true, "FROM": true, "WHERE": true, "GROUP": true, "BY": true, "ORDER": true,
	"HAVING": true, "JOIN": true, "ON": true, "AS": true, "AND": true, "OR": true, "NOT": true,
	"IN": true, "IS": true, "NULL": true, "LIKE": true, "BETWEEN": true, "CASE": true,
	"WHEN": true, "THEN": true, "ELSE": true, "END": true, "TOP": true, "DISTINCT": true,
	"WITH": true, "UNION": true, "ALL": true, "EXCEPT": true, "INTERSECT": true, "ASC": true,
	"DESC": true, "INNER": true, "LEFT": true, "RIGHT": true, "FULL": true, "OUTER": true,
	"CROSS": true, "APPLY": true, "OVER": true, "PARTITION": true, "CAST": true, "TRY_CAST": true,
	"CONVERT": true, "COALESCE": true, "NULLIF": true, "COUNT": true, "SUM": true, "AVG": true,
	"MIN": true, "MAX": true, "ROW_NUMBER": true, "RANK": true, "OFFSET": true, "FETCH": true,
	"ROWS": true, "ONLY": true, "NEXT": true, "EXISTS": true, "INTO": true, "VALUES": true,
}

// columnsRead reports every column the statement reads, qualified by table, and
// `table.*` for a star. A bare name with several tables in scope is attributed
// to all of them: the caller uses this to decide access, so it must fail closed.
func columnsRead(toks []token, tables []string, aliases map[string]string) []string {
	set := map[string]bool{}
	add := func(s string) { set[s] = true }

	for i, t := range toks {
		if t.kind == tokPunct && t.text == "*" {
			// `p.*` is qualified; a bare `*` covers everything in scope.
			if i >= 2 && toks[i-1].text == "." && toks[i-2].kind == tokWord {
				if table, ok := aliases[toks[i-2].up]; ok {
					add(table + ".*")
					continue
				}
			}
			if i > 0 && (toks[i-1].up == "COUNT" || toks[i-1].text == "(") &&
				i+1 < len(toks) && toks[i+1].text == ")" {
				continue // COUNT(*) reads no named column
			}
			for _, table := range tables {
				add(table + ".*")
			}
			continue
		}
		if t.kind != tokWord || sqlKeywords[t.up] {
			continue
		}
		if nextIs(toks, i, "(") {
			continue // a function name
		}
		if i+1 < len(toks) && toks[i+1].text == "." {
			continue // a qualifier; the column itself is the next word
		}
		name := t.text
		if i >= 2 && toks[i-1].text == "." && toks[i-2].kind == tokWord {
			if table, ok := aliases[toks[i-2].up]; ok {
				add(table + "." + strings.ToLower(name))
				continue
			}
			continue // qualified by something unknown: not a column of ours
		}
		if precededBy(toks, i, "AS") {
			continue // an alias being defined, not a column being read
		}
		if len(tables) == 1 {
			add(tables[0] + "." + strings.ToLower(name))
		} else {
			for _, table := range tables {
				add(table + "." + strings.ToLower(name))
			}
		}
	}
	out := make([]string, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// applyTop enforces the ceiling with T-SQL's own construct, keeping a smaller
// caller-supplied TOP.
// isTopDialect reports whether the row ceiling is written as TOP. Everything
// else this service speaks writes LIMIT, and emitting TOP to a PostgreSQL
// engine produces a syntax error rather than a smaller result — the Python
// executor has always chosen per dialect.
func isTopDialect(dialect string) bool {
	switch strings.ToLower(dialect) {
	case "", "tsql", "mssql", "fabric", "synapse":
		return true
	default:
		return false
	}
}

// trailingLimit matches the ceiling at the END of a statement — the one the
// caller wrote for the whole query, not a LIMIT inside a subquery.
var trailingLimit = regexp.MustCompile(`(?is)\s+LIMIT\s+\d+\s*;?\s*$`)

func applyLimit(sql string, toks []token, p Policy) (string, int) {
	cap := p.MaxRows
	for i, t := range toks {
		if t.kind == tokWord && !t.quoted && t.up == "LIMIT" &&
			i+1 < len(toks) && toks[i+1].kind == tokNumber {
			if n, err := strconv.Atoi(toks[i+1].text); err == nil && n <= cap {
				return sql, n
			}
			break
		}
	}
	trimmed := strings.TrimRight(strings.TrimSpace(sql), "; \t\n\r")
	// Replace the caller's ceiling rather than adding a second one: two LIMIT
	// clauses are a syntax error, so appending would turn "too many rows" into
	// "broken query".
	if trailingLimit.MatchString(trimmed) {
		trimmed = strings.TrimRight(trailingLimit.ReplaceAllString(trimmed, ""), "; \t\n\r")
	}
	return fmt.Sprintf("%s LIMIT %d", trimmed, cap), cap
}

func applyTop(sql string, toks []token, p Policy) (string, int) {
	if !isTopDialect(p.Dialect) {
		return applyLimit(sql, toks, p)
	}
	cap := p.MaxRows
	for i, t := range toks {
		if t.up == "TOP" && i+1 < len(toks) && toks[i+1].kind == tokNumber {
			if n, err := strconv.Atoi(toks[i+1].text); err == nil {
				if n <= cap {
					return sql, n
				}
				return replaceFirstTop(sql, cap), cap
			}
		}
		if t.up == "FROM" {
			break // a TOP after FROM belongs to a subquery
		}
	}
	return insertTop(sql, cap), cap
}

func insertTop(sql string, n int) string {
	upper := strings.ToUpper(sql)
	idx := strings.Index(upper, "SELECT")
	if idx < 0 {
		return sql
	}
	after := idx + len("SELECT")
	rest := strings.TrimLeft(sql[after:], " \t\n\r")
	if strings.HasPrefix(strings.ToUpper(rest), "DISTINCT") {
		gap := len(sql[after:]) - len(rest)
		after += gap + len("DISTINCT")
	}
	return sql[:after] + " TOP " + strconv.Itoa(n) + sql[after:]
}

func replaceFirstTop(sql string, n int) string {
	upper := strings.ToUpper(sql)
	idx := strings.Index(upper, "TOP")
	if idx < 0 {
		return insertTop(sql, n)
	}
	j := idx + 3
	for j < len(sql) && (sql[j] == ' ' || sql[j] == '\t') {
		j++
	}
	k := j
	for k < len(sql) && sql[k] >= '0' && sql[k] <= '9' {
		k++
	}
	return sql[:j] + strconv.Itoa(n) + sql[k:]
}
