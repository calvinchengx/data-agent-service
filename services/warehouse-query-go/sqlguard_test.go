package main

import "testing"

var policy = Policy{Dialect: "tsql", AllowedSchemas: []string{"dbo"}, MaxRows: 500,
	MaxLength: 20000, Database: "contoso_warehouse"}

// The same corpus as tests/test_sqlguard.py and services/conformance/run.py: a
// guard that agrees with the other implementation only on the easy cases is
// not a guard.
func TestAllowed(t *testing.T) {
	for _, sql := range []string{
		"SELECT fiscal_year_label, SUM(revenue_usd) FROM dbo.fct_revenue_summary GROUP BY fiscal_year_label",
		"SELECT TOP 10 * FROM dbo.dim_product ORDER BY list_price_usd DESC",
		"WITH x AS (SELECT * FROM dbo.fct_sales) SELECT COUNT(*) FROM x",
		"SELECT s.amount_usd FROM dbo.fct_sales s JOIN dbo.dim_product p ON p.product_id = s.product_id",
		"SELECT * FROM contoso_warehouse.dbo.dim_country",
		"SELECT a.country FROM dbo.dim_country a UNION SELECT b.country FROM dbo.dim_customer b",
	} {
		if v, err := Guard(sql, policy); err != nil {
			t.Errorf("refused a permitted statement %q: %v", sql, err)
		} else if len(v.Tables) == 0 {
			t.Errorf("%q reported no table", sql)
		}
	}
}

func TestRefused(t *testing.T) {
	cases := map[string]string{
		"DROP TABLE dbo.fct_sales":                    "read-only",
		"SELECT 1; DROP TABLE dbo.fct_sales":          "one statement",
		"DELETE FROM dbo.fct_sales":                   "read-only",
		"UPDATE dbo.fct_sales SET amount_usd = 0":     "read-only",
		"INSERT INTO dbo.fct_sales VALUES (1)":        "read-only",
		"SELECT * INTO dbo.copy FROM dbo.fct_sales":   "read-only",
		"TRUNCATE TABLE dbo.fct_sales":                "read-only",
		"EXEC xp_cmdshell 'dir'":                      "read-only",
		"SELECT * FROM OPENROWSET('a','b','c')":       "not allowed",
		"SELECT * FROM other.fct_sales":               "not queryable",
		"SELECT * FROM otherdb.dbo.fct_sales":         "cross-database",
		"SELECT * FROM fct_sales":                     "schema-qualified",
		"SELECT 1":                                    "reads no table",
		"":                                            "empty",
	}
	for sql, want := range cases {
		_, err := Guard(sql, policy)
		if err == nil {
			t.Errorf("permitted a statement that must be refused: %q", sql)
			continue
		}
		if !contains(err.Error(), want) {
			t.Errorf("%q refused for the wrong reason: got %q, want it to mention %q",
				sql, err.Error(), want)
		}
	}
}

func TestRowCeiling(t *testing.T) {
	v, err := Guard("SELECT * FROM dbo.fct_sales", policy)
	if err != nil || v.RowLimit != 500 || !contains(v.SQL, "TOP 500") {
		t.Fatalf("ceiling not applied: %+v %v", v, err)
	}
	v, _ = Guard("SELECT TOP 5 * FROM dbo.fct_sales", policy)
	if v.RowLimit != 5 {
		t.Errorf("a smaller caller limit should be kept, got %d", v.RowLimit)
	}
	v, _ = Guard("SELECT TOP 100000 * FROM dbo.fct_sales", policy)
	if v.RowLimit != 500 || !contains(v.SQL, "TOP 500") {
		t.Errorf("a larger caller limit should be capped, got %d (%s)", v.RowLimit, v.SQL)
	}
}

func TestColumnsRead(t *testing.T) {
	v, _ := Guard("SELECT c.email FROM dbo.dim_customer c", policy)
	if !has(v.Columns, "dbo.dim_customer.email") {
		t.Errorf("qualified column not reported: %v", v.Columns)
	}
	v, _ = Guard("SELECT * FROM dbo.dim_customer", policy)
	if !has(v.Columns, "dbo.dim_customer.*") {
		t.Errorf("star not reported: %v", v.Columns)
	}
	v, _ = Guard("SELECT customer_id FROM dbo.dim_customer WHERE email = 'x'", policy)
	if !has(v.Columns, "dbo.dim_customer.email") {
		t.Errorf("a column read in WHERE is still read: %v", v.Columns)
	}
	// Ambiguity fails closed: attributed to every table in scope.
	v, _ = Guard("SELECT email FROM dbo.dim_customer c JOIN dbo.dim_party p ON p.email = c.email", policy)
	if !has(v.Columns, "dbo.dim_customer.email") || !has(v.Columns, "dbo.dim_party.email") {
		t.Errorf("ambiguous column should reach every table: %v", v.Columns)
	}
}

func TestTablesReported(t *testing.T) {
	v, _ := Guard("SELECT * FROM dbo.fct_sales s JOIN dbo.dim_product p ON p.product_id=s.product_id", policy)
	if len(v.Tables) != 2 || v.Tables[0] != "dbo.dim_product" || v.Tables[1] != "dbo.fct_sales" {
		t.Errorf("tables wrong: %v", v.Tables)
	}
}

func contains(haystack, needle string) bool {
	return len(needle) == 0 || (len(haystack) >= len(needle) && indexOf(haystack, needle) >= 0)
}

func indexOf(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}

func has(values []string, want string) bool {
	for _, v := range values {
		if v == want {
			return true
		}
	}
	return false
}
