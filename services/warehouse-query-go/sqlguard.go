package main

// The SQL guard — a pure function, deliberately NOT a service.
//
// A guard is only trustworthy where it cannot be bypassed, so it runs in the
// same process that holds the database connection. Nothing reaches the
// executor's cursor without passing through Guard.
//
// Policy, in order of severity:
//
//  1. one statement only (a parser that accepts `; DROP TABLE` is not a guard);
//  2. the root must be a SELECT (CTEs allowed);
//  3. no DDL/DML/permission node anywhere in the tree, `SELECT … INTO` included;
//  4. no denied function or procedure (OPENROWSET, xp_*, …);
//  5. every table reference resolves inside an allowed schema of THIS source —
//     no cross-database three-part names, no linked-server four-part names;
//  6. a row ceiling is enforced by rewriting the query with TOP/LIMIT.
//
// A query that cannot be parsed is refused. That is the whole point: the guard
// decides on a tree it understands, never on a scan over text it does not.
//
// This file used to do the last part with a tokeniser and a set of positional
// rules — it recognised the shapes it knew were dangerous and let the rest
// through. That is backwards, and it shipped a real bypass: `FROM a,
// other.secrets` returned rows from a schema the policy forbade while the
// audit line recorded only the first table, because the scan stopped at the
// comma. It now walks a parse tree from github.com/calvinchengx/sqlglot-go,
// which is a port of the same sqlglot the Python executor uses, verified
// against it statement by statement. The two implementations agree because
// they read the same grammar, not because both were tested on the same
// examples.

import (
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"github.com/calvinchengx/sqlglot-go/sqlglot"
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

// Node classes that must never appear, whatever the dialect spells them. The
// parser recognises a write far enough to name it and refuses to build a tree
// for it, so most arrive as an error rather than a node; these catch the ones
// that can hide inside a query the parser does build.
var forbiddenNodes = map[string]bool{
	"Insert": true, "Update": true, "Delete": true, "Merge": true,
	"Drop": true, "Create": true, "Alter": true, "TruncateTable": true,
	"Grant": true, "Command": true, "Transaction": true, "Commit": true,
	"Rollback": true, "Use": true, "Set": true,
}

// Callables that read or write outside the query's own tables, or run code.
var deniedCalls = map[string]bool{
	"openrowset": true, "openquery": true, "opendatasource": true,
	"openxml": true, "bulk": true, "sp_executesql": true,
	"xp_cmdshell": true, "sp_oacreate": true, "sp_send_dbmail": true,
}

var deniedPrefixes = []string{"xp_", "sp_", "fn_trace", "sys.fn_"}

// Guard decides whether a statement may run, and returns the statement that
// actually will.
func Guard(sql string, p Policy) (*Verdict, error) {
	if strings.TrimSpace(sql) == "" {
		return nil, denied("empty statement")
	}
	if p.MaxLength > 0 && len(sql) > p.MaxLength {
		return nil, denied("statement is longer than %d characters", p.MaxLength)
	}

	tree, err := sqlglot.ParseOne(sql, p.Dialect)
	if err != nil {
		return nil, parseRefusal(err, p)
	}

	// 2. the root must be a SELECT, or a CTE or set operation over SELECTs.
	root := tree
	if root.Class == "Subquery" {
		root = root.This()
	}
	switch root.Class {
	case "Select", "Union", "Except", "Intersect":
	default:
		return nil, denied("only SELECT is allowed; this endpoint is read-only (got %s)",
			strings.ToUpper(root.Class))
	}

	if err := refuseForbiddenNodes(tree); err != nil {
		return nil, err
	}
	if err := refuseDeniedCalls(tree, p); err != nil {
		return nil, err
	}
	if err := refuseFunctionsUsedAsTables(tree, p); err != nil {
		return nil, err
	}

	tables, err := tablesRead(tree, p)
	if err != nil {
		return nil, err
	}
	if len(tables) == 0 {
		return nil, denied("the query reads no table")
	}

	// Read the columns before the ceiling is applied: capping a set operation
	// wraps it in `SELECT * FROM (…)`, and that star is the guard's own, not
	// something the caller asked to read.
	columns := columnsRead(tree, tables)

	rowLimit, capped, err := applyCeiling(root, p)
	if err != nil {
		return nil, err
	}
	if capped != nil {
		tree = capped
	}
	rewritten, err := sqlglot.Generate(tree, p.Dialect)
	if err != nil {
		return nil, denied("could not rewrite the statement for %s: %v", p.Dialect, err)
	}

	return &Verdict{
		SQL:      rewritten,
		Tables:   tables,
		Columns:  columns,
		RowLimit: rowLimit,
	}, nil
}

// parseRefusal turns the parser's error into the reason a caller needs. A
// write and a second statement are refused for what they are; anything else
// the parser cannot read is refused for that.
func parseRefusal(err error, p Policy) error {
	var notAQuery *sqlglot.NotAQueryError
	if errors.As(err, &notAQuery) {
		return denied("only SELECT is allowed; this endpoint is read-only (got %s)",
			statementClass(notAQuery.Kind))
	}
	if errors.Is(err, sqlglot.ErrMultipleStatements) {
		return denied("exactly one statement is allowed")
	}
	return denied("could not parse as %s: %v", p.Dialect, err)
}

// statementClass names a statement the way the Python guard names it: after
// the node class sqlglot would have built, not after the keyword. The two
// differ for exactly two statements, and a caller comparing the executors'
// refusals should not have to know which one answered.
var statementClasses = map[string]string{
	"TRUNCATE": "TRUNCATETABLE",
	"EXEC":     "EXECUTE",
}

func statementClass(keyword string) string {
	if class, ok := statementClasses[keyword]; ok {
		return class
	}
	return keyword
}

func refuseForbiddenNodes(tree *sqlglot.Expression) error {
	var err error
	tree.Walk(func(n *sqlglot.Expression) bool {
		if err != nil {
			return false
		}
		if forbiddenNodes[n.Class] {
			err = denied("%s is not allowed; this endpoint is read-only", strings.ToUpper(n.Class))
			return false
		}
		// SELECT … INTO is a query that writes, and only the tree says so.
		if n.Class == "Select" && n.Args["into"] != nil {
			err = denied("SELECT … INTO writes a table; this endpoint is read-only")
			return false
		}
		return true
	})
	return err
}

func refuseDeniedCalls(tree *sqlglot.Expression, p Policy) error {
	var err error
	tree.Walk(func(n *sqlglot.Expression) bool {
		if err != nil {
			return false
		}
		name, isFunc := sqlglot.FunctionName(n, p.Dialect)
		if !isFunc {
			return true
		}
		lower := strings.ToLower(name)
		if deniedCalls[lower] {
			err = denied("function %s is not allowed", lower)
			return false
		}
		for _, prefix := range deniedPrefixes {
			if strings.HasPrefix(lower, prefix) {
				err = denied("function %s is not allowed", lower)
				return false
			}
		}
		return true
	})
	return err
}

// refuseFunctionsUsedAsTables covers APPLY and LATERAL over a callable.
//
// `CROSS APPLY other.f(1)` produces rows from a function in a schema this
// source does not allow, and there is no Table node anywhere in it — so the
// schema rule below never sees it. A subquery there is legitimate and its own
// tables are checked normally, so only the function form is refused.
func refuseFunctionsUsedAsTables(tree *sqlglot.Expression, p Policy) error {
	for _, lateral := range tree.FindAll("Lateral") {
		inner := lateral.This()
		if inner == nil || inner.Class == "Subquery" || inner.Class == "Select" {
			continue
		}
		return denied("%s is a function used as a table; only tables in %s may be read",
			renderNode(inner, p), quotedSchemas(p))
	}
	return nil
}

// tablesRead is rule 5: every table reference resolves inside an allowed
// schema of this source.
func tablesRead(tree *sqlglot.Expression, p Policy) ([]string, error) {
	allowed := map[string]bool{}
	for _, s := range p.AllowedSchemas {
		allowed[strings.ToLower(s)] = true
	}
	ctes := map[string]bool{}
	for _, cte := range tree.FindAll("CTE") {
		if alias, _ := cte.Args["alias"].(*sqlglot.Expression); alias != nil {
			ctes[strings.ToLower(alias.This().Name())] = true
		}
	}

	seen := map[string]bool{}
	for _, t := range tree.FindAll("Table") {
		// A CTE name is a Table node too; those are not real references.
		if ctes[strings.ToLower(t.This().Name())] {
			continue
		}

		// A TABLE FUNCTION is not a table. DuckDB will read a file as a
		// relation -- read_csv_auto('/etc/passwd'), read_parquet('s3://…'),
		// glob('/**') -- and so will other engines under other names. Those
		// parse as a Table whose `this` is a call rather than an identifier.
		// Discriminating on the node type rather than on a list of names
		// covers the functions no one has thought of yet, which is the only
		// kind that matters.
		if this := t.This(); this == nil || this.Class != "Identifier" {
			return nil, denied("%s is a table function, not a table; only tables in %s may be read",
				renderNode(this, p), quotedSchemas(p))
		}

		catalog, schema, name := tableName(t)
		switch {
		case catalog != "" && p.Database != "" && !strings.EqualFold(catalog, p.Database):
			return nil, denied("cross-database reference %s.%s.%s is not allowed", catalog, schema, name)
		case catalog != "" && p.Database == "":
			return nil, denied("three-part name %s.%s.%s is not allowed", catalog, schema, name)
		case schema == "":
			return nil, denied("table %s must be schema-qualified (e.g. %s.%s)",
				name, sortedSchemas(p)[0], name)
		case !allowed[strings.ToLower(schema)]:
			return nil, denied("schema %s is not queryable; allowed: %s",
				schema, strings.Join(sortedSchemas(p), ", "))
		}
		seen[schema+"."+name] = true
	}

	out := make([]string, 0, len(seen))
	for t := range seen {
		out = append(out, t)
	}
	sort.Strings(out)
	return out, nil
}

// tableName splits a Table node into catalog, schema and name; the missing
// parts come back empty.
func tableName(t *sqlglot.Expression) (catalog, schema, name string) {
	parts := []string{}
	for _, key := range []string{"catalog", "db"} {
		if p, _ := t.Args[key].(*sqlglot.Expression); p != nil {
			parts = append(parts, p.Name())
		}
	}
	parts = append(parts, t.This().Name())
	switch len(parts) {
	case 3:
		return parts[0], parts[1], parts[2]
	case 2:
		return "", parts[0], parts[1]
	default:
		return "", "", parts[0]
	}
}

// columnsRead is every column the statement reads, qualified by table.
//
// A column named in WHERE or GROUP BY has been read as surely as one in the
// projection, so the whole tree is walked rather than the select list. Where a
// bare column name could belong to more than one table in scope, one candidate
// per table is reported: the caller of this decides access, and it must fail
// closed rather than guess.
func columnsRead(tree *sqlglot.Expression, tables []string) []string {
	inScope := map[string]bool{}
	for _, t := range tables {
		inScope[t] = true
	}
	byAlias := map[string]string{}
	for _, t := range tree.FindAll("Table") {
		_, schema, name := tableName(t)
		qualified := name
		if schema != "" {
			qualified = schema + "." + name
		}
		if !inScope[qualified] {
			continue
		}
		key := name
		if alias, _ := t.Args["alias"].(*sqlglot.Expression); alias != nil {
			key = alias.This().Name()
		}
		byAlias[strings.ToLower(key)] = qualified
	}

	out := map[string]bool{}
	for _, star := range tree.FindAll("Star") {
		targets := tables
		if parent := star.Parent; parent != nil && parent.Class == "Column" {
			if q, _ := parent.Args["table"].(*sqlglot.Expression); q != nil {
				if resolved, ok := byAlias[strings.ToLower(q.Name())]; ok {
					targets = []string{resolved}
				}
			}
		}
		for _, t := range targets {
			out[t+".*"] = true
		}
	}
	for _, column := range tree.FindAll("Column") {
		name := column.This().Name()
		if name == "" || name == "*" {
			continue
		}
		qualifier := ""
		if q, _ := column.Args["table"].(*sqlglot.Expression); q != nil {
			qualifier = strings.ToLower(q.Name())
		}
		switch {
		case qualifier != "" && byAlias[qualifier] != "":
			out[byAlias[qualifier]+"."+name] = true
		case len(tables) == 1:
			out[tables[0]+"."+name] = true
		default:
			for _, t := range tables {
				out[t+"."+name] = true
			}
		}
	}

	columns := make([]string, 0, len(out))
	for c := range out {
		columns = append(columns, c)
	}
	sort.Strings(columns)
	return columns
}

// applyCeiling enforces the row ceiling by rewriting the tree, keeping a
// smaller caller-supplied limit if there is one. Which construct the dialect
// writes -- TOP in front, LIMIT at the end -- is the generator's business.
//
// A set operation is the exception, and the reason this is not simply "set
// the limit arg": T-SQL has no LIMIT, and TOP belongs to a SELECT, so a
// capped UNION has to be wrapped in one. sqlglot does the same, and the two
// executors have to send the engine the same statement.
func applyCeiling(root *sqlglot.Expression, p Policy) (int, *sqlglot.Expression, error) {
	cap := p.MaxRows
	existing, _ := root.Args["limit"].(*sqlglot.Expression)
	current, err := callerLimit(existing)
	if err != nil {
		return 0, nil, err
	}
	if current > 0 && current <= cap {
		return current, nil, nil
	}

	limit := sqlglot.New("Limit",
		sqlglot.Arg{Key: "this", Value: nil},
		sqlglot.Arg{Key: "expression", Value: sqlglot.New("Literal",
			sqlglot.Arg{Key: "this", Value: strconv.Itoa(cap)},
			sqlglot.Arg{Key: "is_string", Value: false})},
		sqlglot.Arg{Key: "limit_options", Value: nil},
		sqlglot.Arg{Key: "expressions", Value: nil},
	)

	if root.Class != "Select" && topDialect(p) {
		// TOP belongs to a SELECT, so a capped set operation is wrapped in
		// one. The alias is sqlglot's, so both executors name it the same.
		wrapped := newSelect()
		wrapped.Set("expressions", []*sqlglot.Expression{newStar()})
		wrapped.Set("limit", limit)
		wrapped.Set("from_", sqlglot.New("From", sqlglot.Arg{Key: "this", Value: sqlglot.New("Subquery",
			sqlglot.Arg{Key: "this", Value: root},
			sqlglot.Arg{Key: "alias", Value: sqlglot.New("TableAlias",
				sqlglot.Arg{Key: "this", Value: sqlglot.New("Identifier",
					sqlglot.Arg{Key: "this", Value: "_l_0"},
					sqlglot.Arg{Key: "quoted", Value: false})},
				sqlglot.Arg{Key: "columns", Value: nil})},
			sqlglot.Arg{Key: "sample", Value: nil})}))
		return cap, wrapped, nil
	}

	root.Set("limit", limit)
	return cap, nil, nil
}

// newSelect builds a Select with the argument order the reference constructs
// it with, so a tree the guard assembles dumps and writes like a parsed one.
func newSelect() *sqlglot.Expression {
	sel := sqlglot.New("Select")
	for _, key := range []string{
		"kind", "hint", "distinct", "expressions", "limit", "exclude", "operation_modifiers",
	} {
		sel.Set(key, nil)
	}
	return sel
}

func newStar() *sqlglot.Expression {
	return sqlglot.New("Star",
		sqlglot.Arg{Key: "ilike", Value: nil}, sqlglot.Arg{Key: "except_", Value: nil},
		sqlglot.Arg{Key: "replace", Value: nil}, sqlglot.Arg{Key: "rename", Value: nil})
}

// topDialect reports whether the dialect writes its row ceiling as TOP.
func topDialect(p Policy) bool {
	cfg, ok := sqlglot.ConfigFor(p.Dialect)
	return ok && cfg.Tables.LimitIsTop
}

// callerLimit is the row limit the caller asked for, or 0 if there isn't one.
//
// A PERCENTAGE is refused rather than read: `TOP 100 PERCENT` returns every
// row, and taking its literal as a count made the guard believe a statement
// was capped at 100 while the engine returned the whole table. Refused rather
// than silently replaced, because a caller who asked for a proportion and
// received 500 rows was answered a different question than they asked -- and
// the message tells the agent what to write instead.
func callerLimit(limit *sqlglot.Expression) (int, error) {
	if limit == nil {
		return 0, nil
	}
	if options, _ := limit.Args["limit_options"].(*sqlglot.Expression); options != nil {
		if options.Args["percent"] == true {
			return 0, denied("TOP … PERCENT is a proportion, not a row ceiling; use TOP n")
		}
	}
	value, _ := limit.Args["expression"].(*sqlglot.Expression)
	n, err := strconv.Atoi(value.Name())
	if err != nil {
		return 0, nil // a non-literal limit is treated as absent
	}
	return n, nil
}

// quotedSchemas renders the allow-list the way the Python guard does, so the
// two executors' refusals are the same string and not merely the same idea.
func quotedSchemas(p Policy) string {
	out := sortedSchemas(p)
	for i, s := range out {
		out[i] = "'" + s + "'"
	}
	return "[" + strings.Join(out, ", ") + "]"
}

func sortedSchemas(p Policy) []string {
	out := append([]string(nil), p.AllowedSchemas...)
	sort.Strings(out)
	if len(out) == 0 {
		return []string{"(none)"}
	}
	return out
}

// renderNode names a node in a refusal message, in the caller's own dialect
// where the generator can write it.
func renderNode(n *sqlglot.Expression, p Policy) string {
	if n == nil {
		return "?"
	}
	if s, err := sqlglot.Generate(n, p.Dialect); err == nil && s != "" {
		if len(s) > 60 {
			return s[:60]
		}
		return s
	}
	return n.Class
}
