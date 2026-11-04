package api

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/audit"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/connpayload"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dbtls"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/language"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd/gatewaymodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pydconv"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// routes/connections.py: the inspection of a connection through the engine (the tests, the
// files a folder takes, the tables and columns, the Google Sheets and the API call tried),
// and the placeholders' live preview.

func init() {
	register("connections_inspect", func(d *Deps) []server.Route {
		a := func(route string, h authedFunc) http.Handler { return server.Authed(d.Auth, route, h) }
		b := func(route string, h authedFunc) http.Handler { return withBody(a(route, h)) }
		return []server.Route{
			{Method: http.MethodPost, Pattern: "/connections/{conn_id}/test", Handler: b("/connections/{conn_id}/test", testConnection(d))},
			{Method: http.MethodPost, Pattern: "/connections/{conn_id}/sftp/files", Handler: b("/connections/{conn_id}/sftp/files", listSFTPFiles(d))},
			{Method: http.MethodPost, Pattern: "/connections/{conn_id}/api/try", Handler: b("/connections/{conn_id}/api/try", tryAPICall(d))},
			{Method: http.MethodPost, Pattern: "/connections/{conn_id}/sharepoint/files",
				Handler: b("/connections/{conn_id}/sharepoint/files", listSharePointFiles(d))},
			{Method: http.MethodPost, Pattern: "/connections/{conn_id}/gsheets/spreadsheets",
				Handler: a("/connections/{conn_id}/gsheets/spreadsheets", listGsheetsSpreadsheets(d))},
			{Method: http.MethodPost, Pattern: "/connections/{conn_id}/gsheets/tabs", Handler: b("/connections/{conn_id}/gsheets/tabs", listGsheetsTabs(d))},
			{Method: http.MethodPost, Pattern: "/connections/{conn_id}/gsheets/preview",
				Handler: b("/connections/{conn_id}/gsheets/preview", previewGsheetsTab(d))},
			{Method: http.MethodPost, Pattern: "/connections/{conn_id}/s3/files", Handler: b("/connections/{conn_id}/s3/files", listS3Files(d))},
			{Method: http.MethodPost, Pattern: "/placeholders/preview", Handler: b("/placeholders/preview", previewPlaceholders(d))},
			{Method: http.MethodGet, Pattern: "/connections/{conn_id}/tables", Handler: a("/connections/{conn_id}/tables", listConnectionTables(d))},
			{Method: http.MethodGet, Pattern: "/connections/{conn_id}/columns", Handler: a("/connections/{conn_id}/columns", listConnectionColumns(d))},
		}
	})
}

// ── the engine's inspection (_engine_inspect) ─────────────────────────────────

const sqlServerDocs = "https://github.com/JumpAsAService/Tabularia/blob/main/docs/sqlserver.md"

var driverLabels = map[string]string{"sqlserver": "SQL Server"}

var driverNotInstalled = map[string]string{
	"en": "The {label} driver is not installed in this installation: the published images do not include it, " +
		"because it is Microsoft's, under Microsoft's licence. Whoever runs Tabularia can add it: {docs}",
	"it": "Il driver {label} non è installato in questa installazione: le immagini pubblicate non lo includono, " +
		"perché è di Microsoft, sotto la licenza di Microsoft. Chi gestisce Tabularia può aggiungerlo: {docs}",
	"de": "Der {label}-Treiber ist in dieser Installation nicht installiert: Die veröffentlichten Images enthalten ihn nicht, " +
		"weil er von Microsoft stammt und unter Microsofts Lizenz steht. Wer Tabularia betreibt, kann ihn hinzufügen: {docs}",
	"fr": "Le pilote {label} n'est pas installé dans cette installation : les images publiées ne l'incluent pas, " +
		"car il est de Microsoft, sous la licence de Microsoft. Qui gère Tabularia peut l'ajouter : {docs}",
	"es": "El controlador de {label} no está instalado en esta instalación: las imágenes publicadas no lo incluyen, " +
		"porque es de Microsoft, bajo la licencia de Microsoft. Quien gestiona Tabularia puede añadirlo: {docs}",
}

var hostKeyChanged = map[string]string{
	"en": "The host key of {host} has changed: accepted {expected}, offered now {offered}. Nothing was sent to the server. " +
		"If the change is expected (server reinstalled, key renewed), run «Test connection» again to accept the new key; " +
		"otherwise ask the server's administrator.",
	"it": "La chiave dell'host {host} è cambiata: accettata {expected}, offerta ora {offered}. Al server non è stato mandato nulla. " +
		"Se il cambio è atteso (server reinstallato, chiave rinnovata), rifai «Prova connessione» per accettare la nuova chiave; " +
		"altrimenti chiedi all'amministratore del server.",
	"de": "Der Host-Schlüssel von {host} hat sich geändert: akzeptiert {expected}, jetzt angeboten {offered}. An den Server wurde nichts gesendet. " +
		"Ist die Änderung erwartet (Server neu installiert, Schlüssel erneuert), führen Sie «Verbindung testen» erneut aus, um den neuen Schlüssel " +
		"zu akzeptieren; andernfalls fragen Sie den Administrator des Servers.",
	"fr": "La clé d'hôte de {host} a changé : acceptée {expected}, proposée maintenant {offered}. Rien n'a été envoyé au serveur. " +
		"Si le changement est attendu (serveur réinstallé, clé renouvelée), relancez « Tester la connexion » pour accepter la nouvelle clé ; " +
		"sinon, demandez à l'administrateur du serveur.",
	"es": "La clave de host de {host} ha cambiado: aceptada {expected}, ofrecida ahora {offered}. No se ha enviado nada al servidor. " +
		"Si el cambio es esperado (servidor reinstalado, clave renovada), vuelva a ejecutar «Probar conexión» para aceptar la nueva clave; " +
		"si no, pregunte al administrador del servidor.",
}

var hostKeyUnknown = map[string]string{
	"en": "The host key of {host} has not been accepted yet: run «Test connection» on the connection to see it and accept it.",
	"it": "La chiave dell'host {host} non è ancora stata accettata: fai «Prova connessione» sulla connessione per vederla e accettarla.",
	"de": "Der Host-Schlüssel von {host} wurde noch nicht akzeptiert: Führen Sie «Verbindung testen» für die Verbindung aus, um ihn zu sehen und zu akzeptieren.",
	"fr": "La clé d'hôte de {host} n'a pas encore été acceptée : lancez « Tester la connexion » sur la connexion pour la voir et l'accepter.",
	"es": "La clave de host de {host} aún no ha sido aceptada: ejecute «Probar conexión» en la conexión para verla y aceptarla.",
}

var hostKeyNotTheTestedOne = map[string]string{
	"en": "{host} now offers the key {offered}, not the one your test showed ({expected}): it was not accepted. Run the test again.",
	"it": "{host} ora offre la chiave {offered}, non quella mostrata dalla tua prova ({expected}): non è stata accettata. Rifai la prova.",
	"de": "{host} bietet jetzt den Schlüssel {offered} an, nicht den von Ihrem Test gezeigten ({expected}): Er wurde nicht akzeptiert. Testen Sie erneut.",
	"fr": "{host} propose maintenant la clé {offered}, pas celle montrée par votre test ({expected}) : elle n'a pas été acceptée. Relancez le test.",
	"es": "{host} ofrece ahora la clave {offered}, no la que mostró su prueba ({expected}): no ha sido aceptada. Vuelva a probar.",
}

// translated is _translated: the table's text in the language, its {names} replaced (str()
// of each value, as str.format does).
func translated(table map[string]string, lang string, values ...string) string {
	t, ok := table[lang]
	if !ok {
		t = table["en"]
	}
	pairs := []string{}
	for i := 0; i+1 < len(values); i += 2 {
		pairs = append(pairs, "{"+values[i]+"}", values[i+1])
	}
	return strings.NewReplacer(pairs...).Replace(t)
}

// requestLanguage is request_language.
func requestLanguage(r *http.Request) string { return language.Of(r.Header.Get("Accept-Language")) }

// inspectPath is the engine's route _engine_inspect chooses for the payload.
func inspectPath(payload pyjson.Object, action string) string {
	if _, sp := payload.Get("tenant_id"); sp {
		return "/sharepoint/inspect"
	}
	switch payload.Value("db_type") {
	case "s3":
		if action == "files" || action == "validate" {
			return "/s3/inspect"
		}
	case "sftp":
		return "/sftp/inspect"
	case "api":
		return "/http-api/inspect"
	case "gsheets":
		return "/gsheets/inspect"
	}
	return "/db/inspect"
}

// engineInspect is _engine_inspect: the engine's answer (its JSON, as it came), or the
// refusal the gateway makes of its error. `path` goes in the body only towards SharePoint.
func engineInspect(ctx context.Context, d *Deps, payload pyjson.Object, action, path, lang string, extra pyjson.Object) ([]byte, *auth.Refusal, error) {
	route := inspectPath(payload, action)
	body := pyjson.Object{{Key: "connection", Value: payload}, {Key: "action", Value: action}}
	if route == "/sharepoint/inspect" {
		body = append(body, pyjson.KV{Key: "path", Value: path})
	}
	for _, kv := range extra {
		body = body.Set(kv.Key, kv.Value)
	}
	res, raw, err := d.Engine.JSON(ctx, http.MethodPost, route, body)
	if err != nil {
		return nil, nil, err
	}
	if res.StatusCode < 400 {
		return raw, nil, nil
	}
	var detail any = engineText(raw, 1<<30)
	if v, err := pyjson.Loads(string(raw)); err == nil {
		if o, ok := v.(pyjson.Object); ok {
			if dv, has := o.Get("detail"); has {
				detail = dv
			}
		}
	}
	if o, ok := detail.(pyjson.Object); ok {
		switch code := o.Value("code"); code {
		case "driver_not_installed":
			dbType := pyjson.Str(pyjson.Or(pyjson.Or(o.Value("db_type"), payload.Value("db_type")), ""))
			label, docs := dbType, sqlServerDocs
			if l, ok := driverLabels[dbType]; ok {
				label = l
			}
			if dl, ok := driverDocs[dbType]; ok {
				docs = dl
			}
			return nil, unprocessable(translated(driverNotInstalled, lang, "label", label, "docs", docs)), nil
		case "host_key_changed", "host_key_unknown":
			table := hostKeyChanged
			if code == "host_key_unknown" {
				table = hostKeyUnknown
			}
			host := o.Value("host")
			if !pyjson.Truthy(host) {
				host = payload.Value("host")
				if _, has := payload.Get("host"); !has {
					host = ""
				}
			}
			text := translated(table, lang, "host", pyjson.Str(host), "expected", pyjson.Str(pyjson.Or(o.Value("expected"), "—")),
				"offered", pyjson.Str(pyjson.Or(o.Value("offered"), "—")))
			return nil, &auth.Refusal{Status: http.StatusConflict, Detail: text}, nil
		}
	}
	return nil, &auth.Refusal{Status: res.StatusCode, Detail: runes(pyjson.Str(detail), 500)}, nil
}

// inspectAnswer answers the engine's inspection as the route returns it.
func inspectAnswer(d *Deps, w http.ResponseWriter, raw []byte, ref *auth.Refusal, err error) {
	if err != nil {
		fail500(d, w, "connections", err)
		return
	}
	if ref != nil {
		server.Refuse(w, ref)
		return
	}
	engineJSON(w, raw)
}

// ── the saved connection, read for an inspection ─────────────────────────────

// usedConnection is _get_connection after the parameters, then CONNECT on its folder.
func usedConnection(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, v any) (*connpayload.Conn, bool) {
	id, ok := rowID(v)
	var conn *connpayload.Conn
	var err error
	if ok {
		if conn, err = connpayload.Load(r.Context(), d.DB, id); err != nil {
			fail500(d, w, "connections", err)
			return nil, true
		}
	}
	if conn == nil {
		server.Refuse(w, notFound("Connection not found"))
		return nil, true
	}
	if ref, err := perm.For(d.DB, &p.User).Ensure(r.Context(), conn.ProjectID, perm.Connect); refuse(d, w, ref, err) {
		return nil, true
	}
	return conn, false
}

// connectionWithBody validates the path and the body together (FastAPI's order), then reads
// the connection.
func connectionWithBody(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, m *pyd.Model, optional bool) (*connpayload.Conn, *pyd.ModelValue, bool) {
	var ps pyd.Params
	v := ps.Path("conn_id", r.PathValue("conn_id"), pyd.Int())
	var body *pyd.ModelValue
	if m != nil {
		if optional {
			body = ps.OptionalBody(bodyOf(r), m)
		} else {
			body = ps.Body(bodyOf(r), m)
		}
	}
	if !validated(w, &ps) {
		return nil, nil, true
	}
	conn, done := usedConnection(d, w, r, p, v)
	return conn, body, done
}

// pyDump is model_dump() (Python's mode) turned into the values pyjson writes: as Dump, but a
// float field still holding its default int stays an int, as Python's dump leaves it.
func pyDump(mv *pyd.ModelValue) pyjson.Object {
	out := pydconv.FromPyd(mv.Dump()).(pyjson.Object)
	for _, f := range mv.Model.Fields() {
		v := mv.Get(f.Name)
		switch x := v.(type) {
		case *pyd.ModelValue:
			out = out.Set(f.Name, pyDump(x))
		}
	}
	return out
}

// templateFields is FileTemplatesSpec.template_fields: only what differs from a plain file
// datasource.
func templateFields(mv *pyd.ModelValue) pyjson.Object {
	out := pyjson.Object{}
	tz, _ := mv.Get("timezone").(string)
	if t := pystr.Strip(tz); t != "" && t != "UTC" {
		out = append(out, pyjson.KV{Key: "timezone", Value: t})
	}
	if vars, _ := mv.Get("variables").([]any); len(vars) > 0 {
		list := make([]any, len(vars))
		for i, v := range vars {
			list[i] = pyDump(v.(*pyd.ModelValue))
		}
		out = append(out, pyjson.KV{Key: "variables", Value: list})
	}
	if lri, _ := mv.Get("last_refresh_initial").(string); pystr.Strip(lri) != "" {
		out = append(out, pyjson.KV{Key: "last_refresh_initial", Value: pystr.Strip(lri)})
	}
	if mode, _ := mv.Get("mode").(string); mode != "snapshot" {
		out = append(out, pyjson.KV{Key: "mode", Value: mode})
	}
	keys := []any{}
	ks, _ := mv.Get("keys").([]any)
	for _, k := range ks {
		if s := pystr.Strip(k.(string)); s != "" {
			keys = append(keys, s)
		}
	}
	if len(keys) > 0 {
		out = append(out, pyjson.KV{Key: "keys", Value: keys})
	}
	return out
}

func strField(mv *pyd.ModelValue, name string) string {
	s, _ := mv.Get(name).(string)
	return s
}

// ── the routes ───────────────────────────────────────────────────────────────

// testConnection is POST /connections/{conn_id}/test: a saved connection tested. SFTP is
// trust on first use: the key a test shows is stored only once the user accepts it.
func testConnection(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		conn, body, done := connectionWithBody(d, w, r, p, gatewaymodels.HostKeyAcceptance, true)
		if done {
			return
		}
		ctx, lang := r.Context(), requestLanguage(r)
		if conn.DBType != "sftp" {
			payload := connpayload.Payload(conn)
			if body != nil && body.Get("extra") != nil && dbtls.IsDB(conn.DBType) {
				raw, ref := validExtra(pyStr(body.Get("extra")), conn.DBType)
				if ref != nil {
					server.Refuse(w, ref)
					return
				}
				payload = payload.Set("extra", dbtls.EngineOptions(conn.DBType, raw))
			}
			raw, ref, err := engineInspect(ctx, d, payload, "test", "", lang, nil)
			inspectAnswer(d, w, raw, ref, err)
			return
		}
		expected, expectedType := "", ""
		if body != nil {
			expected, expectedType = pystr.Strip(strField(body, "expected_fingerprint")), pystr.Strip(strField(body, "expected_type"))
		}
		address := conn.Host + ":" + portOr(conn.Port, 22)
		raw, ref, err := engineInspect(ctx, d, connpayload.Payload(conn), "test", "", lang,
			pyjson.Object{{Key: "expected_fingerprint", Value: expected}, {Key: "expected_type", Value: expectedType}})
		if err != nil || ref != nil {
			inspectAnswer(d, w, raw, ref, err)
			return
		}
		result, ok := sftpResult(w, raw)
		if !ok {
			return
		}
		if ref, ok := refuseUnofferedKey(result, expected, address, lang); !ok {
			server.InternalError(w)
			return
		} else if ref != nil {
			server.Refuse(w, ref)
			return
		}
		if pyjson.Truthy(result.Value("needs_acceptance")) || !pyjson.Truthy(result.Value("ok")) {
			engineJSON(w, raw) // shown, not accepted: no credential was sent
			return
		}
		seenV := pyjson.Or(result.Value("host_key"), pyjson.Object{})
		seen, isDict := seenV.(pyjson.Object)
		if !isDict {
			server.InternalError(w) // .get on something else
			return
		}
		offered := pyjson.Str(pyjson.Or(seen.Value("fingerprint"), ""))
		keyType := pyjson.Str(pyjson.Or(seen.Value("type"), ""))
		if offered == "" {
			server.Refuse(w, &auth.Refusal{Status: http.StatusBadGateway, Detail: "SFTP: the engine did not report the server's host key"})
			return
		}
		previous := conn.HostKeyFingerprint
		if conn.HostKeyType != keyType || previous != offered {
			if _, err := d.DB.Exec(ctx, `UPDATE connections SET host_key_type = $1, host_key_fingerprint = $2, updated_at = $3 WHERE id = $4`,
				keyType, offered, time.Now().UTC(), conn.ID); err != nil {
				fail500(d, w, "connections", err)
				return
			}
			kind := "connection"
			audit.Record(ctx, d.DB, d.Log, audit.Event{Actor: &p.User, Action: "connection.host_key_accept", TargetType: &kind,
				TargetID: &conn.ID, TargetLabel: &conn.Name, Detail: pyjson.Object{{Key: "type", Value: keyType},
					{Key: "fingerprint", Value: offered}, {Key: "previous", Value: previous}, {Key: "host", Value: address}}})
		}
		out := append(pyjson.Object{}, result...)
		out = out.Set("changed", previous != "" && previous != offered)
		out = out.Set("previous", previous)
		out = out.Set("accepted", true)
		server.JSON(w, http.StatusOK, out)
	}
}

// portOr is f"{port or default}".
func portOr(port *int64, def int64) string {
	if port == nil || *port == 0 {
		return fmt.Sprint(def)
	}
	return fmt.Sprint(*port)
}

// sftpResult is the engine's answer to an SFTP test as a dict: anything else is the 500 of
// the attribute error Python meets on `.get`; a body that is not JSON, json.loads raising.
func sftpResult(w http.ResponseWriter, raw []byte) (pyjson.Object, bool) {
	v, err := pyjson.Loads(string(raw))
	o, ok := v.(pyjson.Object)
	if err != nil || !ok {
		server.InternalError(w)
		return nil, false
	}
	return o, true
}

// refuseUnofferedKey is _refuse_unoffered_key: the key accepted is not the one the server
// offers (any more). ok false: Python's AttributeError (a host_key that is not a dict).
func refuseUnofferedKey(result pyjson.Object, expected, address, lang string) (*auth.Refusal, bool) {
	if expected == "" || !pyjson.Truthy(result.Value("needs_acceptance")) {
		return nil, true
	}
	hk := pyjson.Or(result.Value("host_key"), pyjson.Object{})
	o, isDict := hk.(pyjson.Object)
	if !isDict {
		return nil, false
	}
	offered := pyjson.Str(pyjson.Or(o.Value("fingerprint"), "—"))
	return &auth.Refusal{Status: http.StatusConflict, Detail: translated(hostKeyNotTheTestedOne, lang, "host", address,
		"offered", offered, "expected", expected)}, true
}

// listSFTPFiles is POST /connections/{conn_id}/sftp/files: what a folder + glob takes.
func listSFTPFiles(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		conn, body, done := connectionWithBody(d, w, r, p, gatewaymodels.SftpFilesQuery, false)
		if done {
			return
		}
		if conn.DBType != "sftp" {
			server.Refuse(w, unprocessable("This is not an SFTP connection"))
			return
		}
		folder, pattern := strField(body, "folder"), strField(body, "pattern")
		extra := pyjson.Object{{Key: "folder", Value: folder}, {Key: "pattern", Value: pattern}}
		fields := templateFields(body)
		if len(fields) > 0 || strings.ContainsAny(folder+pattern, "{}") {
			// placeholders or variables: the engine expands them and lists the values without a file
			source := pyjson.Object{{Key: "folder", Value: folder}, {Key: "pattern", Value: pattern}}
			for _, kv := range fields {
				source = source.Set(kv.Key, kv.Value)
			}
			extra = append(extra, pyjson.KV{Key: "source", Value: source})
		}
		raw, ref, err := engineInspect(r.Context(), d, connpayload.Payload(conn), "files", "", requestLanguage(r), extra)
		inspectAnswer(d, w, raw, ref, err)
	}
}

// tryAPICall is POST /connections/{conn_id}/api/try: the first page of the first call.
func tryAPICall(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		conn, body, done := connectionWithBody(d, w, r, p, gatewaymodels.ApiCallSpec, false)
		if done {
			return
		}
		if conn.DBType != "api" {
			server.Refuse(w, unprocessable("This is not an API connection"))
			return
		}
		raw, ref, err := engineInspect(r.Context(), d, connpayload.Payload(conn), "try", "", "en",
			pyjson.Object{{Key: "source", Value: apiDump(body)}})
		inspectAnswer(d, w, raw, ref, err)
	}
}

// apiDump is ApiCallSpec.model_dump(): ApiLimits.min_interval_seconds, a float field whose
// default is the int 0, stays 0 while the input did not set it.
func apiDump(mv *pyd.ModelValue) pyjson.Object {
	out := pyDump(mv)
	if _, set := mv.Get("limits").(*pyd.ModelValue); !set {
		if limits, ok := out.Value("limits").(pyjson.Object); ok {
			out = out.Set("limits", append(pyjson.Object{}, limits...).Set("min_interval_seconds", int64(0)))
		}
	}
	return out
}

// listSharePointFiles is POST /connections/{conn_id}/sharepoint/files.
func listSharePointFiles(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		conn, body, done := connectionWithBody(d, w, r, p, gatewaymodels.SharePointFilesQuery, false)
		if done {
			return
		}
		if conn.DBType != "sharepoint" {
			server.Refuse(w, unprocessable("This is not a SharePoint connection"))
			return
		}
		path := strField(body, "path")
		source := pyjson.Object{{Key: "path", Value: path}, {Key: "format", Value: strField(body, "format")}}
		for _, kv := range templateFields(body) {
			source = source.Set(kv.Key, kv.Value)
		}
		raw, ref, err := engineInspect(r.Context(), d, connpayload.Payload(conn), "files", path, "en",
			pyjson.Object{{Key: "source", Value: source}})
		inspectAnswer(d, w, raw, ref, err)
	}
}

// gsheetsConnection is _gsheets_connection.
func gsheetsConnection(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal, m *pyd.Model) (*connpayload.Conn, *pyd.ModelValue, bool) {
	conn, body, done := connectionWithBody(d, w, r, p, m, false)
	if done {
		return nil, nil, true
	}
	if conn.DBType != "gsheets" {
		server.Refuse(w, unprocessable("This is not a Google Sheets connection"))
		return nil, nil, true
	}
	return conn, body, false
}

// listGsheetsSpreadsheets is POST /connections/{conn_id}/gsheets/spreadsheets.
func listGsheetsSpreadsheets(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		conn, _, done := gsheetsConnection(d, w, r, p, nil)
		if done {
			return
		}
		raw, ref, err := engineInspect(r.Context(), d, connpayload.Payload(conn), "spreadsheets", "", "en", nil)
		inspectAnswer(d, w, raw, ref, err)
	}
}

// listGsheetsTabs is POST /connections/{conn_id}/gsheets/tabs.
func listGsheetsTabs(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		conn, body, done := gsheetsConnection(d, w, r, p, gatewaymodels.GsheetsTabsQuery)
		if done {
			return
		}
		raw, ref, err := engineInspect(r.Context(), d, connpayload.Payload(conn), "tabs", "", "en",
			pyjson.Object{{Key: "spreadsheet", Value: pystr.Strip(strField(body, "spreadsheet"))}})
		inspectAnswer(d, w, raw, ref, err)
	}
}

// previewGsheetsTab is POST /connections/{conn_id}/gsheets/preview.
func previewGsheetsTab(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		conn, body, done := gsheetsConnection(d, w, r, p, gatewaymodels.GsheetsTab)
		if done {
			return
		}
		raw, ref, err := engineInspect(r.Context(), d, connpayload.Payload(conn), "preview", "", "en",
			pyjson.Object{{Key: "source", Value: pyDump(body)}})
		inspectAnswer(d, w, raw, ref, err)
	}
}

// listS3Files is POST /connections/{conn_id}/s3/files: «Check the files» of an S3 folder.
func listS3Files(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		conn, body, done := connectionWithBody(d, w, r, p, gatewaymodels.S3FilesQuery, false)
		if done {
			return
		}
		if conn.DBType != "s3" {
			server.Refuse(w, unprocessable("This is not an S3 connection"))
			return
		}
		bucket := pystr.Strip(strField(body, "bucket"))
		if bucket == "" && pystr.Strip(conn.Database) == "" {
			server.Refuse(w, unprocessable("No bucket: set one on the datasource or as the connection's default"))
			return
		}
		source := pyjson.Object{{Key: "bucket", Value: bucket}, {Key: "folder", Value: strings.Trim(pystr.Strip(strField(body, "folder")), "/")},
			{Key: "pattern", Value: pystr.Strip(strField(body, "pattern"))}}
		for _, kv := range templateFields(body) {
			source = source.Set(kv.Key, kv.Value)
		}
		raw, ref, err := engineInspect(r.Context(), d, connpayload.Payload(conn), "files", "", "en",
			pyjson.Object{{Key: "source", Value: source}})
		inspectAnswer(d, w, raw, ref, err)
	}
}

// listConnectionTables is GET /connections/{conn_id}/tables.
func listConnectionTables(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		conn, _, done := connectionWithBody(d, w, r, p, nil, false)
		if done {
			return
		}
		raw, ref, err := engineInspect(r.Context(), d, connpayload.Payload(conn), "tables", "", requestLanguage(r), nil)
		inspectAnswer(d, w, raw, ref, err)
	}
}

// listConnectionColumns is GET /connections/{conn_id}/columns: a table's columns.
func listConnectionColumns(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		v := ps.Path("conn_id", r.PathValue("conn_id"), pyd.Int())
		table := ps.Query(pyd.ParseQuery(r.URL.RawQuery), "table", pyd.Str().MinLen(1).MaxLen(500))
		if !validated(w, &ps) {
			return
		}
		conn, done := usedConnection(d, w, r, p, v)
		if done {
			return
		}
		if connpayload.NotDatabases[conn.DBType] || conn.DBType == "s3" {
			server.Refuse(w, unprocessable("This connection has no tables"))
			return
		}
		raw, ref, err := engineInspect(r.Context(), d, connpayload.Payload(conn), "columns", "", requestLanguage(r),
			pyjson.Object{{Key: "table", Value: table}})
		inspectAnswer(d, w, raw, ref, err)
	}
}

// previewPlaceholders is POST /placeholders/preview: each template's value now, by the
// engine's checker. A Pushmail cell's names in the language of whoever asks.
func previewPlaceholders(d *Deps) authedFunc {
	return func(w http.ResponseWriter, r *http.Request, p *auth.Principal) {
		var ps pyd.Params
		body := ps.Body(bodyOf(r), gatewaymodels.PlaceholderPreviewQuery)
		if !validated(w, &ps) {
			return
		}
		payload := pyjson.Object{}
		for _, kv := range pyDump(body) {
			if kv.Key != "pushmail" {
				payload = append(payload, kv)
			}
		}
		if body.Get("pushmail") == true {
			payload = append(payload, pyjson.KV{Key: "lang", Value: emailLanguage(&p.User)})
		}
		res, raw, err := d.Engine.JSON(r.Context(), http.MethodPost, "/placeholders/preview", payload)
		if err != nil {
			fail500(d, w, "placeholders", err)
			return
		}
		if res.StatusCode >= 400 {
			status := http.StatusUnprocessableEntity
			if res.StatusCode >= 500 {
				status = http.StatusBadGateway
			}
			server.Refuse(w, &auth.Refusal{Status: status, Detail: engineText(raw, 500)})
			return
		}
		engineJSON(w, raw)
	}
}

// emailLanguage is services/pushmail.py email_language: the language the user's emails are
// written in.
func emailLanguage(u *auth.User) string {
	if u.Locale != nil {
		switch *u.Locale {
		case "en", "it", "fr", "de", "es":
			return *u.Locale
		}
	}
	return "en"
}
