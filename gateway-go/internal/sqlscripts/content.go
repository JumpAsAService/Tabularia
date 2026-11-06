package sqlscripts

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

func unprocessable(detail string) *auth.Refusal {
	return &auth.Refusal{Status: http.StatusUnprocessableEntity, Detail: detail}
}

// CheckContent is check_content: the content as it will be saved, or the 422 saying what is
// wrong.
func CheckContent(content any) (pyjson.Object, *auth.Refusal) {
	c, ok := content.(pyjson.Object)
	if !ok {
		return nil, unprocessable("The script's content is not an object")
	}
	sqlV, has := c.Get("sql")
	if !has {
		sqlV = ""
	}
	sql, ok := sqlV.(string)
	if !ok {
		return nil, unprocessable("The script's SQL is a text")
	}
	if utf8.RuneCountInString(sql) > MaxSQLChars {
		return nil, unprocessable("The SQL is too long: at most 100,000 characters")
	}
	engine, _ := c.Get("engine")
	if !Truthy(engine) {
		engine = nil
	}
	if engine != nil {
		e, isText := engine.(string)
		if !isText || !KnownEngines[e] {
			return nil, unprocessable("Unknown engine: " + pyjson.Str(engine))
		}
	}
	connection, _ := c.Get("connection_id")
	if connection != nil {
		if _, isInt := connection.(int64); !isInt {
			return nil, unprocessable("The script's connection is a number")
		}
	}
	outputV, _ := c.Get("output")
	var output any
	if Truthy(outputV) {
		o, isObj := outputV.(pyjson.Object)
		if !isObj {
			return nil, unprocessable("The script's datasource is an object: a name and a folder")
		}
		nameV, _ := o.Get("name")
		name := ""
		if Truthy(nameV) {
			name = strings.TrimSpace(pyjson.Str(nameV))
		}
		if name == "" || utf8.RuneCountInString(name) > 200 {
			return nil, unprocessable("The script's datasource needs a name (at most 200 characters)")
		}
		project, _ := o.Get("project_id")
		if _, isInt := project.(int64); !isInt {
			return nil, unprocessable("The script's datasource needs a folder")
		}
		output = pyjson.Object{{Key: "name", Value: name}, {Key: "project_id", Value: project}}
	}
	if connection != nil {
		engine = nil
	}
	return pyjson.Object{{Key: "sql", Value: sql}, {Key: "engine", Value: engine}, {Key: "connection_id", Value: connection},
		{Key: "output", Value: output}}, nil
}

func get(o pyjson.Object, key string) any {
	v, _ := o.Get(key)
	return v
}

// textOr is `(x or "")`, then the text (a stored content's SQL is one).
func textOr(v any) string {
	if !Truthy(v) {
		return ""
	}
	return pyjson.Str(v)
}

// Widens is widens: what a change makes the script read or write that it did not.
func Widens(before, after pyjson.Object) []string {
	var reasons []string
	if strings.TrimSpace(textOr(get(before, "sql"))) != strings.TrimSpace(textOr(get(after, "sql"))) {
		reasons = append(reasons, "its SQL changed")
	}
	if !pyjson.Equal(get(after, "connection_id"), get(before, "connection_id")) {
		if get(after, "connection_id") != nil {
			reasons = append(reasons, "it runs on another connection")
		} else {
			reasons = append(reasons, "it runs on the catalog instead of its connection")
		}
	}
	b, _ := get(before, "output").(pyjson.Object)
	a, _ := get(after, "output").(pyjson.Object)
	if len(a) > 0 && !pyjson.Equal(get(a, "project_id"), get(b, "project_id")) {
		reasons = append(reasons, "it writes its datasource in another folder")
	}
	return reasons
}

// GroupsUsing is groups_using: the flow groups with a cell that writes the script's
// datasource, in the database's order.
func GroupsUsing(ctx context.Context, q Querier, scriptID int64) ([]flowgroups.Group, error) {
	groups, err := flowgroups.Containing(ctx, q, `"sql"`)
	if err != nil {
		return nil, err
	}
	var out []flowgroups.Group
	for _, g := range groups {
		def := flowgroups.DefinitionOf(g.Definition)
	found:
		for _, row := range def.Steps {
			for _, c := range row {
				if c != nil && c.Kind == "sql" && c.ID == scriptID {
					out = append(out, g)
					break found
				}
			}
		}
	}
	return out, nil
}

// sqlScriptLock is _SQL_SCRIPT_LOCK: the advisory locks of «start a SQL script's write».
const sqlScriptLock = 74610005 // 74610004 is the flow groups queue's

// InLine is in_line_for_the_script: released by the transaction's end.
func InLine(ctx context.Context, tx pgx.Tx, scriptID int64) error {
	_, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1, $2)`, sqlScriptLock, scriptID)
	return err
}

// LiveRun is live_run: whether a write of the script is still queued or running.
func LiveRun(ctx context.Context, q Querier, scriptID int64) (bool, error) {
	var id int64
	err := q.QueryRow(ctx, `SELECT id FROM runs WHERE sql_script_id = $1 AND kind = 'sql'
		AND status NOT IN ('SUCCESS', 'FAILURE') ORDER BY id DESC`, scriptID).Scan(&id)
	if err == pgx.ErrNoRows {
		return false, nil
	}
	return err == nil, err
}

// CheckOutput is sql_script_runs.check_output: the output can be written by the person.
func CheckOutput(ctx context.Context, c *perm.Checker, q Querier, user *auth.User, output pyjson.Object) (*auth.Refusal, error) {
	project, _ := OutputProject(output)
	name := pyjson.Str(get(output, "name"))
	var one int
	err := q.QueryRow(ctx, `SELECT 1 FROM projects WHERE id = $1`, project).Scan(&one)
	if err == pgx.ErrNoRows {
		return &auth.Refusal{Status: http.StatusNotFound, Detail: "The folder of the script's datasource no longer exists: choose another"}, nil
	}
	if err != nil {
		return nil, err
	}
	ok, err := c.Has(ctx, project, perm.Edit)
	if err != nil {
		return nil, err
	}
	if !ok {
		return &auth.Refusal{Status: http.StatusForbidden, Detail: fmt.Sprintf("%s cannot write in the folder of the script's datasource «%s»",
			user.Email, name)}, nil
	}
	target, err := TargetOf(ctx, q, output)
	if err != nil {
		return nil, err
	}
	if target != nil && target.Kind != "flow" {
		return &auth.Refusal{Status: http.StatusConflict, Detail: fmt.Sprintf("«%s» is a datasource of kind «%s»: the script cannot "+
			"overwrite it, choose another name", name, target.Kind)}, nil
	}
	return nil, nil
}
