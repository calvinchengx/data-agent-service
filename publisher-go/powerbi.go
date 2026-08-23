package publisher

import (
	"fmt"
	"strings"
)

// The Power BI spelling: publisher/model.py and publisher/report.py. Every
// constant below is the Python's, by value; the test holds the bytes.
const (
	OneLake            = "https://onelake.dfs.fabric.microsoft.com"
	Expression         = "SourceWarehouse"
	CompatibilityLevel = 1604
	PBIRSchema         = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
)

var visualTypes = map[string]string{"card": "card", "bar": "barChart", "table": "tableEx"}

// M is a JSON object. Maps rather than structs so that Canonical sorts the
// keys the way Python's sort_keys does; a struct would keep field order.
type M = map[string]any

func DAXExpression(m Measure) string {
	if m.Function == "COUNTROWS" {
		return fmt.Sprintf("COUNTROWS('%s')", m.Entity())
	}
	return fmt.Sprintf("%s('%s'[%s])", m.Function, m.Entity(), m.Column)
}

func modelTable(table string, columns []Column, measures []Measure) M {
	schema, name, _ := strings.Cut(table, ".")
	entity := EntityOf(table)
	if name == "" {
		schema = "dbo"
	}
	cols := make([]any, 0, len(columns))
	for _, c := range columns {
		dt, ok := c["dataType"]
		if !ok {
			dt = "string"
		}
		cols = append(cols, M{"name": c["name"], "dataType": dt, "sourceColumn": c["name"]})
	}
	ms := []any{}
	for _, m := range measures {
		if m.Table == table {
			ms = append(ms, M{"name": m.Name, "expression": DAXExpression(m)})
		}
	}
	return M{
		"name":     entity,
		"columns":  cols,
		"measures": ms,
		"partitions": []any{M{
			"name": entity,
			"mode": "directLake",
			"source": M{
				"type":             "entity",
				"entityName":       entity,
				"schemaName":       schema,
				"expressionSource": Expression,
			},
		}},
	}
}

func TMSL(p Plan, workspace, warehouse string) M {
	tables := []any{}
	for _, t := range p.SortedTables() {
		tables = append(tables, modelTable(t, p.Columns[t], p.Measures))
	}
	return M{
		"name":               p.Name,
		"compatibilityLevel": CompatibilityLevel,
		"model": M{
			"culture": "en-US",
			"expressions": []any{M{
				"name": Expression,
				"kind": "m",
				"expression": fmt.Sprintf(
					"let\n    Source = AzureStorage.DataLake(\"%s/%s/%s\")\nin\n    Source",
					OneLake, workspace, warehouse),
			}},
			"tables": tables,
		},
	}
}

func field(entity, column string) M {
	return M{"Column": M{"Expression": M{"SourceRef": M{"Entity": entity}}, "Property": column}}
}

func measureRef(entity, name string) M {
	return M{"Measure": M{"Expression": M{"SourceRef": M{"Entity": entity}}, "Property": name}}
}

func Binding(modelName string) M {
	return M{
		"version":          "1.0",
		"datasetReference": M{"byPath": M{"path": fmt.Sprintf("../%s.SemanticModel", modelName)}},
	}
}

func Layout(p Plan) M {
	entity := p.Entity()
	category := []any{}
	for _, d := range p.Dimensions {
		category = append(category, M{"queryRef": d[0] + "." + d[1], "field": field(d[0], d[1])})
	}
	y := []any{}
	for _, m := range p.Measures {
		y = append(y, M{"queryRef": entity + "." + m.Name, "field": measureRef(entity, m.Name)})
	}
	containers := []any{M{
		"x": 0, "y": 0, "width": 960, "height": 480,
		"config": M{
			"name":  "answer",
			"title": p.Title,
			"singleVisual": M{
				"visualType":  visualTypes[p.Visual],
				"projections": M{"Category": category, "Y": y},
			},
		},
	}}
	// A slicer per recorded slot, each one empty: the column is known because
	// the promoter kept it; the value is not, because it deliberately did not.
	for i, s := range p.Slicers {
		containers = append(containers, M{
			"x": 0, "y": 480 + i*120, "width": 320, "height": 100,
			"config": M{
				"name": "slicer-" + s[1],
				"singleVisual": M{
					"visualType": "slicer",
					"projections": M{"Values": []any{
						M{"queryRef": s[0] + "." + s[1], "field": field(s[0], s[1])},
					}},
				},
			},
		})
	}
	return M{
		"$schema": PBIRSchema + "/report/1.0.0/schema.json",
		"sections": []any{M{
			"name":             "page1",
			"displayName":      p.Title,
			"width":            1280,
			"height":           720,
			"visualContainers": containers,
		}},
	}
}

// DAX is the query the verification runs: SUMMARIZECOLUMNS with the same
// grouping and the same aggregate, the closest DAX gets to the SELECT.
func DAX(p Plan) (string, error) {
	owned := p.ColumnNames()
	groups := []string{}
	for _, d := range p.Dimensions {
		owner, err := TableOf(d[1], p.Tables, owned)
		if err != nil {
			return "", err
		}
		groups = append(groups, fmt.Sprintf("'%s'[%s]", EntityOf(owner), d[1]))
	}
	projected := []string{}
	for _, m := range p.Measures {
		projected = append(projected, fmt.Sprintf("%q, [%s]", m.Name, m.Name))
	}
	inner := strings.Join(projected, ", ")
	if len(groups) > 0 {
		inner = strings.Join(groups, ", ") + ", " + inner
	}
	return "EVALUATE SUMMARIZECOLUMNS(" + inner + ")", nil
}

// PowerBIArtefacts is publisher/targets/powerbi.py's artefacts(): every
// definition the target emits, by the path the contract records it under.
func PowerBIArtefacts(p Plan, workspace, warehouse string) (map[string]any, error) {
	dax, err := DAX(p)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"model.bim":       TMSL(p, workspace, warehouse),
		"report.json":     Layout(p),
		"definition.pbir": Binding(p.Name),
		"query.dax":       M{"dax": dax},
	}, nil
}
