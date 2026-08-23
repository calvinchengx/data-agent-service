// Which columns carry which catalog tag. The Go half of access.py's TagIndex.
//
// The executor has never needed OpenMetadata -- the agent reads it, the
// harnesses read it, this service only ever read its own configuration. A rule
// that denies by tag changes that, so the cost is stated rather than
// discovered: the catalog is now in this service's availability path.
//
// Two consequences, both deliberate and both matching the Python executor:
// the refresh is a BACKGROUND interval rather than a per-request fetch, so a
// slow catalog cannot become query latency; and a first read that fails is
// fatal to serving rather than survivable.
package main

import (
	"errors"
	"fmt"
	"log/slog"
	neturl "net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ErrTagsUnavailable means the catalog could not be read and no tag set has
// ever been read. Distinct from "no column carries that tag", which is a
// legitimate answer -- this one means the executor does not KNOW, and a
// service that does not know what to withhold must not answer questions.
var ErrTagsUnavailable = errors.New("catalog tags unavailable")

type TagIndex struct {
	base     string
	token    string
	refresh  time.Duration
	mu       sync.RWMutex
	byTag    map[string]map[string]bool
	readOnce bool
	at       time.Time
}

func NewTagIndex() *TagIndex {
	seconds := 300
	if raw := os.Getenv("DAS_TAG_REFRESH_S"); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil && n > 0 {
			seconds = n
		}
	}
	return &TagIndex{
		base:    strings.TrimSuffix(os.Getenv("DAS_OM_URL"), "/"),
		token:   os.Getenv("DAS_OM_BOT_TOKEN"),
		refresh: time.Duration(seconds) * time.Second,
		byTag:   map[string]map[string]bool{},
	}
}

// How many tables to ask for at a time, and how many pages to follow before
// concluding something is wrong. A large estate is normal; an endless one is not.
const (
	tagPageSize = 1000
	tagMaxPages = 200
)

type omTables struct {
	Paging struct {
		After string `json:"after"`
	} `json:"paging"`
	Data []struct {
		FullyQualifiedName string `json:"fullyQualifiedName"`
		Columns            []struct {
			Name string `json:"name"`
			Tags []struct {
				TagFQN string `json:"tagFQN"`
			} `json:"tags"`
		} `json:"columns"`
	} `json:"data"`
}

// indexColumnsByTag maps a tag FQN to the columns carrying it, named the way a
// query names them (`schema.table.column`) so the result can be used as a
// deny_columns entry without translation.
//
// COLUMN tags only. OpenMetadata also tags tables and propagates tags through
// lineage; a table tag would withhold every column of that table, a far larger
// blast radius than the syntax suggests. That is a separate decision with its
// own witness rather than a silent consequence of this one.
func indexColumnsByTag(payload omTables) map[string]map[string]bool {
	byTag := map[string]map[string]bool{}
	for _, table := range payload.Data {
		parts := strings.Split(table.FullyQualifiedName, ".")
		short := table.FullyQualifiedName
		if len(parts) >= 2 {
			short = strings.Join(parts[len(parts)-2:], ".")
		}
		for _, column := range table.Columns {
			if column.Name == "" {
				continue
			}
			for _, label := range column.Tags {
				if label.TagFQN == "" {
					continue
				}
				if byTag[label.TagFQN] == nil {
					byTag[label.TagFQN] = map[string]bool{}
				}
				byTag[label.TagFQN][strings.ToLower(short+"."+column.Name)] = true
			}
		}
	}
	return byTag
}

func (t *TagIndex) fetch() (map[string]map[string]bool, error) {
	token, err := resolveRef(t.token)
	if err != nil {
		return nil, fmt.Errorf("%w: %w", ErrTagsUnavailable, err)
	}
	if t.base == "" || token == "" {
		return nil, fmt.Errorf("%w: no catalog configured (DAS_OM_URL / DAS_OM_BOT_TOKEN)",
			ErrTagsUnavailable)
	}
	// FOLLOWED TO THE END, not read once. The limit was an arbitrary 1000 --
	// the API's ceiling is a million and it returns an `after` cursor -- so a
	// catalog with more tables than one page silently returned the first page
	// and every tagged column past it was silently NOT withheld. A partial
	// read is a security downgrade that looks exactly like a healthy service.
	found := map[string]map[string]bool{}
	after, pages := "", 0
	for {
		url := fmt.Sprintf("%s/api/v1/tables?limit=%d&fields=columns,tags", t.base, tagPageSize)
		if after != "" {
			url += "&after=" + neturl.QueryEscape(after)
		}
		var payload omTables
		if err := getJSON(url, token, &payload); err != nil {
			return nil, fmt.Errorf("%w: cannot read the catalog at %s: %w",
				ErrTagsUnavailable, t.base, err)
		}
		for tag, columns := range indexColumnsByTag(payload) {
			if found[tag] == nil {
				found[tag] = map[string]bool{}
			}
			for column := range columns {
				found[tag][column] = true
			}
		}
		after = payload.Paging.After
		pages++
		if after == "" {
			return found, nil
		}
		if pages >= tagMaxPages {
			// Refusing beats returning most of the answer: "most of the
			// columns that should be withheld" is not a useful guarantee.
			return nil, fmt.Errorf(
				"%w: the catalog at %s did not finish paginating after %d pages of %d;"+
					" refusing a partial tag set", ErrTagsUnavailable, t.base, pages, tagPageSize)
		}
	}
}

func (t *TagIndex) Refresh() error {
	found, err := t.fetch()
	if err != nil {
		return err
	}
	t.mu.Lock()
	t.byTag, t.readOnce, t.at = found, true, time.Now()
	t.mu.Unlock()
	return nil
}

// Resolve returns the columns those tags withhold.
//
// Refuses rather than returning an empty slice when the catalog has never been
// read: answering with no denials would be a silent downgrade that looks like
// a healthy service, and a startup failure is at least visible.
func (t *TagIndex) Resolve(tags []string) ([]string, error) {
	if len(tags) == 0 {
		return nil, nil
	}
	t.mu.RLock()
	fresh := t.readOnce && time.Since(t.at) < t.refresh
	t.mu.RUnlock()
	if !fresh {
		if err := t.Refresh(); err != nil {
			t.mu.RLock()
			everRead := t.readOnce
			t.mu.RUnlock()
			if !everRead {
				return nil, err
			}
			// A set HAS been read; serving the last known one is the
			// documented last-known behaviour and is never reached cold.
			slog.Warn("catalog unreachable; using the last tag set read", "err", err)
		}
	}
	t.mu.RLock()
	defer t.mu.RUnlock()
	seen := map[string]bool{}
	for _, tag := range tags {
		for column := range t.byTag[tag] {
			seen[column] = true
		}
	}
	out := make([]string, 0, len(seen))
	for column := range seen {
		out = append(out, column)
	}
	sort.Strings(out)
	return out, nil
}

func (t *TagIndex) KnownTags() map[string]bool {
	t.mu.RLock()
	defer t.mu.RUnlock()
	known := make(map[string]bool, len(t.byTag))
	for tag := range t.byTag {
		known[tag] = true
	}
	return known
}
