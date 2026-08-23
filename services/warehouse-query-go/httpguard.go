// The guard for HTTP sources -- what sqlguard.go is for SQL ones.
//
// A port of services/warehouse-query-py/httpguard.py, held to the same
// recorded verdicts by http_parity_test.go. Every safety property in this
// service is a SQL parse tree; an HTTP call has none, so each one is
// translated rather than ported:
//
//	single statement, SELECT only  ->  one operation, safe methods only
//	allowed schemas                ->  allowed collections
//	max_rows, rewritten into tree  ->  item ceiling, written into the query
//	max_length on the statement    ->  a ceiling on the response bytes
//	columns read, from the tree    ->  fields read, from the response schema
//	ambiguous column fails closed  ->  anything the spec omits is refused
//
// The last line matters most. A spec is not documentation here, it is the
// allow-list: an operation, parameter or response field the OpenAPI document
// does not describe is refused.
package main

import (
	"encoding/json"
	"fmt"
	"path"
	"sort"
	"strconv"
	"strings"
)

// safeMethods cannot change state. A spec may mark a POST read-only with
// `x-read-only: true` -- some search endpoints are POST because a query does
// not fit in a URL -- and that is the ONLY way a POST becomes callable.
var safeMethods = map[string]bool{"get": true, "head": true}

// pageSizeNames are the spellings of "how many" across the conventions in the
// wild. The ceiling is imposed by writing one of these; an API that spells it
// otherwise declares `x-page-size-param` instead.
var pageSizeNames = map[string]bool{
	"limit": true, "size": true, "pagesize": true, "page_size": true,
	"per_page": true, "perpage": true, "count": true, "top": true,
	// Retrieval services spell it differently, and the ceiling has to reach
	// them too -- an unbounded top_k is how a context window fills.
	"top_k": true, "topk": true, "k": true, "n": true, "max_results": true,
	"maxresults": true, "num_results": true, "rows": true,
}

// HTTPPolicy is what an HTTP source permits: the counterpart of Policy.
type HTTPPolicy struct {
	Collections []string
	MaxItems    int
	MaxBytes    int
	// MaxRequestBytes is the body's ceiling, the counterpart of MaxLength on a
	// statement. Arguments come from a model; a body it can make arbitrarily
	// large is a request this service should not relay.
	MaxRequestBytes int
	BaseURL         string
}

// HTTPVerdict is one operation, checked, with its ceiling already applied.
//
// URL is built here rather than by the backend so no unchecked string reaches
// an HTTP client -- the same reason Guard returns rewritten SQL rather than
// the caller's text.
type HTTPVerdict struct {
	Operation  string
	Method     string
	URL        string
	Collection string
	Params     [][2]string
	ItemLimit  int
	MaxBytes   int
	// Body is the JSON request body, already checked, empty when the call
	// carries none. A string so the backend can send only what was checked.
	Body   string
	Fields []string // collection.operation.field, for the access rules
}

type HTTPParameter struct {
	Name     string
	Location string // path | query | body
	Required bool
	Kind     string // string | integer | number | boolean
	Enum     []string
}

type HTTPOperation struct {
	OperationID   string
	Method        string
	Path          string
	Collection    string
	Summary       string
	Parameters    []HTTPParameter
	Fields        []string
	PageSizeParam string
}

// collectionOf is which collection an operation belongs to: the tag if the
// spec has one -- that is what a human named it -- and the first non-templated
// path segment otherwise, which is what the URL says.
func collectionOf(p string, tags []any) string {
	if len(tags) > 0 {
		if s, ok := tags[0].(string); ok {
			return s
		}
	}
	for _, segment := range strings.Split(strings.Trim(p, "/"), "/") {
		if segment != "" && !strings.HasPrefix(segment, "{") {
			return segment
		}
	}
	return ""
}

// resolveRef follows a local $ref. Anything else resolves to nothing, which
// fails closed: a field the guard cannot read is a field it cannot vouch for.
func specRef(ref string, spec map[string]any) map[string]any {
	if !strings.HasPrefix(ref, "#/") {
		return nil
	}
	var node any = spec
	for _, part := range strings.Split(ref[2:], "/") {
		m, ok := node.(map[string]any)
		if !ok {
			return nil
		}
		node = m[part]
	}
	if m, ok := node.(map[string]any); ok {
		return m
	}
	return nil
}

func mapAt(m map[string]any, key string) map[string]any {
	if m == nil {
		return nil
	}
	sub, _ := m[key].(map[string]any)
	return sub
}

func stringAt(m map[string]any, key string) string {
	if m == nil {
		return ""
	}
	s, _ := m[key].(string)
	return s
}

// schemaFields flattens a response schema to dotted paths.
//
// Bounded at the reference's depth: a schema that recurses -- a tree of
// comments, say -- would otherwise not terminate, and a response the guard
// cannot finish reading is one it cannot be sure it filtered.
func schemaFields(schema, spec map[string]any, prefix string, depth int) []string {
	if depth > 6 || schema == nil {
		return nil
	}
	if ref := stringAt(schema, "$ref"); ref != "" {
		return schemaFields(specRef(ref, spec), spec, prefix, depth+1)
	}
	_, hasItems := schema["items"]
	if stringAt(schema, "type") == "array" || hasItems {
		return schemaFields(mapAt(schema, "items"), spec, prefix, depth+1)
	}
	props := mapAt(schema, "properties")
	if props == nil {
		return nil
	}
	// Sorted, where the reference relies on Python's insertion-ordered dict.
	// Go's map iteration is randomised, so an unsorted walk would emit a
	// different field order on every run and the recorded verdict would only
	// sometimes match. The corpus records what this produces either way.
	names := make([]string, 0, len(props))
	for name := range props {
		names = append(names, name)
	}
	sort.Strings(names)
	out := []string{}
	for _, name := range names {
		dotted := prefix + name
		out = append(out, dotted)
		sub, _ := props[name].(map[string]any)
		out = append(out, schemaFields(sub, spec, dotted+".", depth+1)...)
	}
	return out
}

// parametersOf reads path and query parameters. A header or cookie parameter
// is not something a question should be able to set: it is transport, and the
// executor owns it.
func parametersOf(raw []any, spec map[string]any) []HTTPParameter {
	out := []HTTPParameter{}
	for _, item := range raw {
		p, _ := item.(map[string]any)
		if ref := stringAt(p, "$ref"); ref != "" {
			p = specRef(ref, spec)
		}
		location := stringAt(p, "in")
		if location != "path" && location != "query" {
			continue
		}
		schema := mapAt(p, "schema")
		kind := stringAt(schema, "type")
		if kind == "" {
			kind = "string"
		}
		required, _ := p["required"].(bool)
		out = append(out, HTTPParameter{
			Name: stringAt(p, "name"), Location: location,
			Required: required, Kind: kind, Enum: enumOf(schema),
		})
	}
	return out
}

func enumOf(schema map[string]any) []string {
	raw, _ := schema["enum"].([]any)
	out := make([]string, 0, len(raw))
	for _, v := range raw {
		out = append(out, fmt.Sprint(v))
	}
	return out
}

// bodyParameters flattens a request body into the same shape as the rest.
//
// A retrieval API is why this exists: `POST /search` with a JSON body is how
// most of them take a query, because a query does not fit in a URL. Treating
// body properties as parameters means one validation rule and one allow-list --
// a body property the spec does not describe is refused exactly as an
// undeclared query parameter is.
//
// Only the top level is declared. A nested object is not refused, it is simply
// not something a caller may set: the guard can only vouch for what it names.
func bodyParameters(op, spec map[string]any) []HTTPParameter {
	body := mapAt(op, "requestBody")
	if ref := stringAt(body, "$ref"); ref != "" {
		body = specRef(ref, spec)
	}
	schema := mapAt(mapAt(mapAt(body, "content"), "application/json"), "schema")
	if ref := stringAt(schema, "$ref"); ref != "" {
		schema = specRef(ref, spec)
	}
	required := map[string]bool{}
	if raw, ok := schema["required"].([]any); ok {
		for _, v := range raw {
			required[fmt.Sprint(v)] = true
		}
	}
	props := mapAt(schema, "properties")
	names := make([]string, 0, len(props))
	for name := range props {
		names = append(names, name)
	}
	sort.Strings(names)
	out := []HTTPParameter{}
	for _, name := range names {
		sub, _ := props[name].(map[string]any)
		if ref := stringAt(sub, "$ref"); ref != "" {
			sub = specRef(ref, spec)
		}
		kind := stringAt(sub, "type")
		if kind == "" {
			kind = "string"
		}
		if kind == "object" || kind == "array" {
			continue // nameable, but not something the guard can vouch for
		}
		out = append(out, HTTPParameter{
			Name: name, Location: "body", Required: required[name],
			Kind: kind, Enum: enumOf(sub),
		})
	}
	return out
}

// LoadSpec indexes an OpenAPI document by operationId.
//
// Only operations this guard could ever permit are indexed -- an unsafe method
// is dropped here rather than refused later, so the surface a caller can even
// NAME is the surface they may use.
func LoadSpec(document map[string]any) map[string]*HTTPOperation {
	operations := map[string]*HTTPOperation{}
	paths := mapAt(document, "paths")
	for p, rawItem := range paths {
		item, _ := rawItem.(map[string]any)
		sharedRaw, _ := item["parameters"].([]any)
		shared := parametersOf(sharedRaw, document)
		for method, rawOp := range item {
			lower := strings.ToLower(method)
			if lower != "get" && lower != "head" && lower != "post" {
				continue
			}
			op, ok := rawOp.(map[string]any)
			if !ok {
				continue
			}
			if readOnly, _ := op["x-read-only"].(bool); lower == "post" && !readOnly {
				continue
			}
			operationID := stringAt(op, "operationId")
			if operationID == "" {
				operationID = lower + "_" + strings.Trim(p, "/")
			}
			ownRaw, _ := op["parameters"].([]any)
			params := append([]HTTPParameter{}, shared...)
			params = append(params, parametersOf(ownRaw, document)...)
			params = append(params, bodyParameters(op, document)...)

			okResp := mapAt(mapAt(op, "responses"), "200")
			content := mapAt(mapAt(okResp, "content"), "application/json")
			fields := schemaFields(mapAt(content, "schema"), document, "", 0)

			page := stringAt(op, "x-page-size-param")
			if page == "" {
				for _, prm := range params {
					if pageSizeNames[strings.ToLower(prm.Name)] {
						page = prm.Name
						break
					}
				}
			}
			summary := stringAt(op, "summary")
			if summary == "" {
				summary = stringAt(op, "description")
			}
			tags, _ := op["tags"].([]any)
			operations[operationID] = &HTTPOperation{
				OperationID: operationID, Method: lower, Path: p,
				Collection: collectionOf(p, tags), Summary: truncate(summary, 300),
				Parameters: params, Fields: fields, PageSizeParam: page,
			}
		}
	}
	return operations
}

// typedValue checks a supplied value against what the spec declared.
func typedValue(value any, p HTTPParameter) (string, error) {
	var text string
	switch v := value.(type) {
	case bool:
		text = "false"
		if v {
			text = "true"
		}
	case string:
		text = v
	case float64:
		// JSON numbers arrive as float64. An integer must not come back as
		// "500.0", which is what %v would give and which no API accepts.
		if v == float64(int64(v)) {
			text = strconv.FormatInt(int64(v), 10)
		} else {
			text = strconv.FormatFloat(v, 'g', -1, 64)
		}
	default:
		text = fmt.Sprint(value)
	}
	if p.Kind == "integer" || p.Kind == "number" {
		if _, err := strconv.ParseFloat(text, 64); err != nil {
			return "", denied("%s must be a %s", p.Name, p.Kind)
		}
	}
	if p.Kind == "boolean" && text != "true" && text != "false" {
		return "", denied("%s must be true or false", p.Name)
	}
	if len(p.Enum) > 0 {
		found := false
		for _, e := range p.Enum {
			if text == e {
				found = true
				break
			}
		}
		if !found {
			return "", denied("%s must be one of %s", p.Name, strings.Join(p.Enum, ", "))
		}
	}
	return text, nil
}

// nativeValue is the validated value in its JSON type.
//
// A query string carries everything as text; a JSON body must not. Sending
// {"top_k": "5"} to an API that declared an integer is the kind of thing that
// works against one implementation and fails against the next.
func nativeValue(text string, p HTTPParameter) any {
	switch p.Kind {
	case "integer":
		f, _ := strconv.ParseFloat(text, 64)
		return int64(f)
	case "number":
		f, _ := strconv.ParseFloat(text, 64)
		return f
	case "boolean":
		return text == "true"
	}
	return text
}

// GuardHTTP checks one operation call and returns what may be executed.
//
// Refuses rather than repairs, and every refusal names the rule: the agent
// reads the reason, and a message that only says "denied" teaches it nothing
// about what to try instead.
func GuardHTTP(operationID string, arguments map[string]any,
	operations map[string]*HTTPOperation, p HTTPPolicy) (*HTTPVerdict, error) {
	op := operations[operationID]
	if op == nil {
		// Single quotes, matching Python's `!r`. The refusal REASON is part of
		// the contract -- the conformance suite compares it, and the agent reads
		// it -- so a quoting difference is a divergence like any other.
		return nil, denied("unknown operation '%s'", operationID)
	}
	if !safeMethods[op.Method] && op.Method != "post" {
		return nil, denied("%s is %s; this endpoint is read-only",
			operationID, strings.ToUpper(op.Method))
	}
	if len(p.Collections) > 0 && !matchesAnyPattern(op.Collection, p.Collections) {
		return nil, denied("collection %s is not queryable", op.Collection)
	}

	declared := map[string]HTTPParameter{}
	for _, prm := range op.Parameters {
		declared[prm.Name] = prm
	}
	supplied := map[string]any{}
	for k, v := range arguments {
		if v != nil {
			supplied[k] = v
		}
	}
	unknown := []string{}
	for k := range supplied {
		if _, ok := declared[k]; !ok {
			unknown = append(unknown, k)
		}
	}
	if len(unknown) > 0 {
		// Fail closed: a parameter the spec does not describe cannot be
		// checked, and an unchecked parameter is the whole attack surface.
		sort.Strings(unknown)
		return nil, denied("unknown parameter(s): %s", strings.Join(unknown, ", "))
	}

	// In declared order, not map order: the query string is built from this
	// and Go's map iteration would put it in a different order every run.
	values := map[string]string{}
	for _, prm := range op.Parameters {
		if raw, ok := supplied[prm.Name]; ok {
			text, err := typedValue(raw, prm)
			if err != nil {
				return nil, err
			}
			values[prm.Name] = text
		} else if prm.Required {
			return nil, denied("%s is required", prm.Name)
		}
	}

	missing := []string{}
	for _, name := range pathParams(op.Path) {
		if _, ok := values[name]; !ok {
			missing = append(missing, name)
		}
	}
	if len(missing) > 0 {
		return nil, denied("missing path parameter(s): %s", strings.Join(missing, ", "))
	}

	// The ceiling, written into the request. CLAMPED rather than rejected: a
	// caller asking for more than the deployment allows gets the deployment's
	// answer, which is how the SQL row ceiling behaves too.
	limit := p.MaxItems
	if op.PageSizeParam != "" {
		if asked, ok := values[op.PageSizeParam]; ok && asked != "" {
			// A NON-POSITIVE page size is not a smaller ceiling, it is no
			// ceiling: plenty of APIs read `limit=0` as unlimited, so a
			// verdict promising 0 items would have fetched every one. Treated
			// as unspecified, which is what the policy's own ceiling is for.
			// Found by fuzzing, in both implementations at once.
			if n, err := strconv.Atoi(asked); err == nil && n > 0 && n < p.MaxItems {
				limit = n
			}
		}
		values[op.PageSizeParam] = strconv.Itoa(limit)
	}

	urlPath := op.Path
	query := [][2]string{}
	bodyValues := map[string]any{}
	for _, prm := range op.Parameters {
		text, ok := values[prm.Name]
		if !ok {
			continue
		}
		switch prm.Location {
		case "path":
			// An EMPTY path parameter collapses its segment, and that changes
			// which endpoint is called: `getInvoice` with `id=""` builds
			// `/invoices/`, the LIST endpoint. The guard authorised
			// `invoices.getInvoice` and the request would have read every
			// invoice -- so a role permitted one could read all of them.
			//
			// Found by fuzzing this guard's own properties, in both
			// implementations at once, exactly as the comma-join bypass was.
			if text == "" {
				return nil, denied("%s is a path parameter and cannot be empty", prm.Name)
			}
			urlPath = strings.ReplaceAll(urlPath, "{"+prm.Name+"}", pyQuote(text))
		case "body":
			bodyValues[prm.Name] = nativeValue(text, prm)
		default:
			query = append(query, [2]string{prm.Name, text})
		}
	}

	body := ""
	if len(bodyValues) > 0 {
		// Compact and key-sorted, matching json.dumps(separators, sort_keys):
		// the body is recorded in the verdict and compared byte for byte.
		b, err := json.Marshal(bodyValues)
		if err != nil {
			return nil, denied("request body could not be built: %v", err)
		}
		body = string(b)
	}
	if len(body) > p.MaxRequestBytes {
		return nil, denied("request body is %d bytes, over the %d ceiling",
			len(body), p.MaxRequestBytes)
	}

	url := strings.TrimRight(p.BaseURL, "/") + urlPath
	if len(query) > 0 {
		parts := make([]string, 0, len(query))
		for _, kv := range query {
			parts = append(parts, pyURLEncode(kv[0])+"="+pyURLEncode(kv[1]))
		}
		url += "?" + strings.Join(parts, "&")
	}

	sorted := append([][2]string{}, query...)
	sort.Slice(sorted, func(i, j int) bool {
		if sorted[i][0] != sorted[j][0] {
			return sorted[i][0] < sorted[j][0]
		}
		return sorted[i][1] < sorted[j][1]
	})
	fields := make([]string, 0, len(op.Fields))
	for _, f := range op.Fields {
		fields = append(fields, op.Collection+"."+op.OperationID+"."+f)
	}
	return &HTTPVerdict{
		Operation: op.OperationID, Method: op.Method, URL: url,
		Collection: op.Collection, Params: sorted, ItemLimit: limit,
		MaxBytes: p.MaxBytes, Body: body, Fields: fields,
	}, nil
}

func pathParams(p string) []string {
	out := []string{}
	for {
		i := strings.Index(p, "{")
		if i < 0 {
			return out
		}
		j := strings.Index(p[i:], "}")
		if j < 0 {
			return out
		}
		out = append(out, p[i+1:i+j])
		p = p[i+j+1:]
	}
}

func matchesAnyPattern(name string, patterns []string) bool {
	for _, pattern := range patterns {
		if ok, err := path.Match(pattern, name); err == nil && ok {
			return true
		}
	}
	return false
}

// pyQuote is urllib.parse.quote(value, safe=""), which is what the reference
// substitutes a path parameter with.
//
// Not url.PathEscape: that leaves `/` alone, and a `/` inside a path parameter
// is how `id=a/../b` climbs out of the collection it was checked against. Not
// url.QueryEscape either -- that writes a space as `+`, which is a query-string
// convention and wrong inside a path.
func pyQuote(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if c := s[i]; pyUnreserved(c) {
			b.WriteByte(c)
		} else {
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// pyURLEncode is quote_plus, which urlencode applies to both halves of a pair:
// the same unreserved set, with a space as `+`.
func pyURLEncode(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c == ' ':
			b.WriteByte('+')
		case pyUnreserved(c):
			b.WriteByte(c)
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// pyUnreserved is Python's `_ALWAYS_SAFE`: letters, digits, and `_.-~`. Go's
// url package has no equivalent set, and the difference is not cosmetic -- a
// character escaped by one and not the other is a different URL.
func pyUnreserved(c byte) bool {
	return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' ||
		c == '_' || c == '.' || c == '-' || c == '~'
}

// FilterResponse strips denied fields from a response, at any depth.
//
// JSON nests, so a field the caller may not read can appear inside an array
// inside an object. Returns the count as well, because the executor reports
// how many were withheld: a caller told "3 fields hidden" knows the answer is
// partial, and one told nothing does not.
func FilterResponse(payload any, denied map[string]bool, depth int) (any, int) {
	if depth > 8 {
		return payload, 0
	}
	switch v := payload.(type) {
	case []any:
		out, n := make([]any, 0, len(v)), 0
		for _, item := range v {
			cleaned, count := FilterResponse(item, denied, depth+1)
			out = append(out, cleaned)
			n += count
		}
		return out, n
	case map[string]any:
		out, n := map[string]any{}, 0
		for key, value := range v {
			if denied[key] {
				n++
				continue
			}
			cleaned, count := FilterResponse(value, denied, depth+1)
			out[key] = cleaned
			n += count
		}
		return out, n
	}
	return payload, 0
}

// TruncateResponse parses a response, refusing one over the ceiling.
//
// Refused rather than truncated: half a JSON document is not a smaller answer,
// it is an unparseable one, and an agent handed malformed data will describe it
// confidently.
func TruncateResponse(raw []byte, maxBytes int) (any, error) {
	if len(raw) > maxBytes {
		return nil, denied("response is %d bytes, over the %d ceiling", len(raw), maxBytes)
	}
	if len(raw) == 0 {
		return nil, nil
	}
	var out any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, denied("response is not JSON: %v", err)
	}
	return out, nil
}
