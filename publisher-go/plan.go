// Package publisher is the Go spelling of publisher/plan.py and the targets
// under publisher/targets/. It exists for one of the two reasons the Go
// executor exists, and not the other: there is no load to compare, but
// "the definitions are deterministic functions of the template" is a claim,
// and a second generator held to the same bytes is what makes it a checked
// one. Where this and the Python disagree, the contract was underspecified,
// which is the finding.
//
// Only the pure part is here -- Plan to artefacts. The REST plumbing (Fabric,
// on-behalf-of, the catalog) stays Python: porting it would be a second
// implementation with nothing to disagree about.
package publisher

import (
	"bytes"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

// Column is one entry of the executor's describe_table output, carried
// through untouched: the contract allows extra keys, so it is a map.
type Column map[string]any

type Measure struct {
	Name     string `json:"name"`
	Table    string `json:"table"`
	Column   string `json:"column"`
	Function string `json:"function"`
}

// Entity is the table's bare name: a target's table is named for the entity,
// not for schema.table.
func (m Measure) Entity() string { return EntityOf(m.Table) }

type Plan struct {
	Name          string              `json:"name"`
	Title         string              `json:"title"`
	Source        string              `json:"source"`
	Tables        []string            `json:"tables"`
	Columns       map[string][]Column `json:"columns"`
	Measures      []Measure           `json:"measures"`
	Dimensions    [][2]string         `json:"dimensions"`
	Slicers       [][2]string         `json:"slicers"`
	Visual        string              `json:"visual"`
	ComparisonSQL string              `json:"comparisonSql"`
}

func EntityOf(table string) string {
	schema, name, found := strings.Cut(table, ".")
	if !found || name == "" {
		return schema
	}
	return name
}

// Entity is the table the answer is read from: the first measure's, or with
// no measure the first table the template read.
func (p Plan) Entity() string {
	if len(p.Measures) > 0 {
		return p.Measures[0].Entity()
	}
	return EntityOf(p.Tables[0])
}

// SortedTables returns the table names in the order the Python emits model
// tables: sorted, because the Plan's columns map is keyed and order-free.
func (p Plan) SortedTables() []string {
	out := make([]string, 0, len(p.Columns))
	for t := range p.Columns {
		out = append(out, t)
	}
	sort.Strings(out)
	return out
}

// ColumnNames is the owner map table_of resolves against.
func (p Plan) ColumnNames() map[string][]string {
	out := map[string][]string{}
	for t, cols := range p.Columns {
		for _, c := range cols {
			out[t] = append(out[t], fmt.Sprint(c["name"]))
		}
	}
	return out
}

// TableOf is plan.table_of: the one table among those the template read that
// owns the column, or an error -- never a guess.
func TableOf(column string, tables []string, owned map[string][]string) (string, error) {
	var owners []string
	for _, t := range tables {
		for _, c := range owned[t] {
			if c == column {
				owners = append(owners, t)
				break
			}
		}
	}
	switch len(owners) {
	case 1:
		return owners[0], nil
	case 0:
		return "", fmt.Errorf("no table in %v has a column %q", tables, column)
	}
	return "", fmt.Errorf("%q is ambiguous across %v; cannot bind it to one table", column, owners)
}

// Canonical is the contract's JSON form: sorted keys, no whitespace, ASCII
// only -- Python's json.dumps(sort_keys=True, separators=(",",":")).
//
// The normalising round-trip through `any` is not ceremony. encoding/json
// sorts MAP keys but keeps STRUCT field order, so a canonical form that took
// the Go type as it found it would give two different strings for the same
// value depending on how the caller happened to declare it -- which is the
// one thing a canonical form must not do. Everything is reduced to maps
// first, and only then encoded.
//
// Two further differences from the default: Go escapes <, > and & for HTML
// where Python leaves them, and Go emits non-ASCII as UTF-8 where Python
// emits \uXXXX. Both are corrected so the generators are compared on the
// same bytes rather than on the same meaning.
func Canonical(v any) (string, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	var normalised any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber() // so 1604 does not come back as 1.604e+03
	if err := dec.Decode(&normalised); err != nil {
		return "", err
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(normalised); err != nil {
		return "", err
	}
	return escapeNonASCII(strings.TrimRight(buf.String(), "\n")), nil
}

func escapeNonASCII(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r < 0x80:
			b.WriteRune(r)
		case r > 0xFFFF:
			r -= 0x10000
			fmt.Fprintf(&b, "\\u%04x\\u%04x", 0xD800+(r>>10), 0xDC00+(r&0x3FF))
		default:
			fmt.Fprintf(&b, "\\u%04x", r)
		}
	}
	return b.String()
}
