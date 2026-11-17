#!/usr/bin/env bash
# Shows the traces of Wattson Agent in your Grafana (GRAFANA_SERVICE in GRAFANA_NAMESPACE,
# settings.env). It adds the Tempo data source "Wattson Tempo" (uid wattson-tempo: the address
# of Tempo in the cluster) and the dashboard "Wattson Agent" (uid wattson,
# k8s/grafana/wattson-dashboard.json) in the folder "wattson". Then it removes what it added
# under the old name, when it is there: the dashboard myagent, the data source myagent-tempo,
# and the folder myagent-folder when nothing else is in it. Without --apply it only prints the
# plan; it runs neither kubectl nor curl.
#
#   bash k8s/grafana.sh            print the plan: the data source, the dashboard, the calls
#   bash k8s/grafana.sh --apply    the same, then you type the name of the context, then it adds
#                                  them, or updates them, through Grafana's HTTP API
#
# RUN IT AGAIN AFTER GRAFANA RESTARTS. The Grafana of monitoring keeps its data in an emptyDir and
# has no provisioning: what this script adds is LOST when that pod is deleted or replaced (a new
# version, a node restart, a helm upgrade). Running it again is always safe: it makes what is
# missing and overwrites the rest (the data source by its uid, the dashboard with "overwrite").
# bash k8s/doctor.sh says when they are missing.
#
# It never restarts Grafana and changes none of its Kubernetes objects. It reads the admin login
# of Grafana from the Secret that its Deployment names (GF_SECURITY_ADMIN_USER and
# GF_SECURITY_ADMIN_PASSWORD) and reaches Grafana with kubectl port-forward on 127.0.0.1. The
# login goes to curl on its standard input: never on a command line, never on screen, never on
# disk.
set -euo pipefail

# shellcheck source=k8s/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

APPLY=no
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=yes ;;
    -h | --help)
      sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "Unknown option: $arg. See: bash k8s/grafana.sh --help" ;;
  esac
done

read_settings
check_settings
[[ "$TELEMETRY" == on ]] || die "TELEMETRY is off in settings.env: there is no Tempo, so nothing to show in Grafana."
command -v python3 >/dev/null 2>&1 || die "python3 is not installed."

# The names in Grafana (lib.sh).
DATASOURCE_UID="$GRAFANA_DATASOURCE_UID" DATASOURCE_NAME="$GRAFANA_DATASOURCE_NAME" DASHBOARD_UID="$GRAFANA_DASHBOARD_UID"
DASHBOARD_TITLE="$GRAFANA_DASHBOARD_TITLE" FOLDER_UID="$GRAFANA_FOLDER_UID" FOLDER_TITLE="$GRAFANA_FOLDER_TITLE"
# What this script added under the old name (before 23 Oct 2026). Removed after the new ones are
# saved: the old dashboard uses the old data source.
OLD_DASHBOARD_UID=myagent OLD_DATASOURCE_UID=myagent-tempo OLD_FOLDER_UID=myagent-folder
DASHBOARD_FILE="$HERE/grafana/wattson-dashboard.json"
[[ -f "$DASHBOARD_FILE" ]] || die "The dashboard file is missing: $DASHBOARD_FILE"

WORK="$(mktemp -d)"
cleanup() {
  grafana_stop
  GRAFANA_LOGIN=""
  rm -rf "$WORK"
}
trap cleanup EXIT

# The data source. Grafana asks Tempo from its own pod, by the address of Tempo's Service.
# Streaming is off: Grafana then uses Tempo's plain HTTP API, which needs nothing more than port
# 3200 (the results are small).
python3 - "$DATASOURCE_UID" "$DATASOURCE_NAME" "$TEMPO_URL" >"$WORK/datasource.json" <<'PY'
import json, sys
uid, name, url = sys.argv[1:]
print(json.dumps({
    "uid": uid,
    "name": name,
    "type": "tempo",
    "access": "proxy",
    "url": url,
    "isDefault": False,
    "jsonData": {"streamingEnabled": {"search": False, "metrics": False}},
}, indent=2))
PY

# The dashboard, as Grafana's API takes it: a new one or the same uid overwritten, in the folder.
python3 - "$DASHBOARD_FILE" "$DASHBOARD_UID" "$DATASOURCE_UID" "$FOLDER_UID" >"$WORK/dashboard.json" 2>"$WORK/dashboard.err" <<'PY' \
  || die "The dashboard file is not right: $(cat "$WORK/dashboard.err")"
import json, sys
path, uid, datasource, folder = sys.argv[1:]
dashboard = json.load(open(path, encoding="utf-8"))
if dashboard.get("uid") != uid:
    sys.exit("its uid must be %s" % uid)
used = {t.get("datasource", {}).get("uid") for p in dashboard["panels"] for t in p.get("targets", [])}
if used != {datasource}:
    sys.exit("every query must use the data source %s, they use %s" % (datasource, sorted(map(str, used))))
dashboard["id"] = None
print(json.dumps({"dashboard": dashboard, "folderUid": folder, "overwrite": True, "message": "k8s/grafana.sh"}))
PY
PANELS="$(python3 -c '
import json, sys
for panel in json.load(open(sys.argv[1]))["dashboard"]["panels"]:
    print("#   " + panel["title"])' "$WORK/dashboard.json")"

info "# The traces of $APP_NAME in Grafana: context $K8S_CONTEXT, namespace $GRAFANA_NAMESPACE, Service $GRAFANA_SERVICE."
info "# Tempo, as Grafana reaches it: $TEMPO_URL"
info "# The data source ($DATASOURCE_NAME, uid $DATASOURCE_UID):"
cat "$WORK/datasource.json"
info "# The dashboard \"$DASHBOARD_TITLE\" (uid $DASHBOARD_UID) in the folder \"$FOLDER_TITLE\", from k8s/grafana/wattson-dashboard.json:"
printf '%s\n' "$PANELS"
info "# The calls, through kubectl port-forward to service/$GRAFANA_SERVICE, as the admin of Grafana:"
info "#   GET  /api/datasources/uid/$DATASOURCE_UID          then PUT there (it is there) or POST /api/datasources"
info "#   GET  /api/folders/$FOLDER_UID                     then POST /api/folders (it is missing)"
info "#   POST /api/dashboards/db                       the dashboard, overwrite: true"
info "#   GET  /api/datasources/uid/$DATASOURCE_UID/health   Grafana asks Tempo (it proves the network rule)"
info "# Then what was added under the old name, removed when it is there:"
info "#   GET  /api/dashboards/uid/$OLD_DASHBOARD_UID             then DELETE there"
info "#   GET  /api/datasources/uid/$OLD_DATASOURCE_UID     then DELETE there"
info "#   GET  /api/folders/$OLD_FOLDER_UID              then GET /api/search?folderUIDs=$OLD_FOLDER_UID, and DELETE"
info "#                                                 /api/folders/$OLD_FOLDER_UID only when nothing is in it"
info "# The Grafana of $GRAFANA_NAMESPACE keeps its data in an emptyDir: after it restarts, run this again."

if [[ "$APPLY" != yes ]]; then
  info ""
  info "Nothing was run. To add them: bash k8s/grafana.sh --apply"
  exit 0
fi

# ---- --apply ----------------------------------------------------------------------------

for tool in kubectl curl base64; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed."
done
seen_context="$(kubectl --context "$K8S_CONTEXT" config view --minify -o 'jsonpath={.contexts[0].name}' 2>/dev/null || true)"
[[ "$seen_context" == "$K8S_CONTEXT" ]] || die "kubectl has no context \"$K8S_CONTEXT\". See: kubectl config get-contexts"

# Before the question: only reads, and only names.
tempo_service="$(k get service "$TEMPO" --ignore-not-found -o name)" \
  || die "kubectl could not look for the Service $TEMPO (its own words are above). Nothing was changed. Check the connection (VPN)."
[[ -n "$tempo_service" ]] || die "There is no Service $TEMPO in \"$K8S_NAMESPACE\": deploy first (bash k8s/deploy.sh, with TELEMETRY=on). Nothing was changed."
grafana_service="$(grafana_k get service "$GRAFANA_SERVICE" --ignore-not-found -o name)" \
  || die "kubectl could not look for the Service $GRAFANA_SERVICE in \"$GRAFANA_NAMESPACE\" (its own words are above). Nothing was changed."
[[ -n "$grafana_service" ]] || die "There is no Service $GRAFANA_SERVICE in \"$GRAFANA_NAMESPACE\" (the settings GRAFANA_NAMESPACE and GRAFANA_SERVICE). Nothing was changed."
login_status=0
grafana_login_refs >/dev/null || login_status=$?
case "$login_status" in
  0) ;;
  2) die "The Deployment $GRAFANA_SERVICE does not take its admin login from a Secret (GF_SECURITY_ADMIN_USER / GF_SECURITY_ADMIN_PASSWORD). Nothing was changed." ;;
  *) die "kubectl could not read the Deployment $GRAFANA_SERVICE in \"$GRAFANA_NAMESPACE\" (its own words are above). Nothing was changed." ;;
esac

info ""
printf 'Type "%s" to add them to the Grafana of %s: ' "$K8S_CONTEXT" "$GRAFANA_NAMESPACE"
answer=""
IFS= read -r answer || true
if [[ "$answer" != "$K8S_CONTEXT" ]]; then
  info ""
  info "Nothing was changed."
  exit 1
fi

grafana_read_login || die "The admin login of Grafana could not be read from its Secret (may your kubectl read Secrets in \"$GRAFANA_NAMESPACE\"?). Nothing was changed."
grafana_forward "$WORK/forward.log" \
  || die "kubectl port-forward to service/$GRAFANA_SERVICE did not start: $(tail -n 2 "$WORK/forward.log" | tr '\n' ' ')"

# api CALL_NAME METHOD PATH [BODY_FILE] -> sets STATUS; the answer is in $WORK/answer-CALL_NAME.json.
api() {
  STATUS="$(grafana_call "$2" "$3" "$WORK/answer-$1.json" "${4:-}")"
}
failed() {  # WHAT CALL_NAME
  die "$1: Grafana answered $STATUS: $(grafana_message "$WORK/answer-$2.json")"
}

# Grafana answers, and takes the login.
for _ in {1..20}; do
  api health GET /api/health
  [[ "$STATUS" == 200 ]] && break
  sleep 0.5
done
[[ "$STATUS" == 200 ]] || die "Grafana does not answer through the port-forward (status $STATUS)."
api user GET /api/user
[[ "$STATUS" == 200 ]] || failed "Grafana refused the admin login of its Secret" user

# The data source: updated by its uid when it is there, made when it is not.
api datasource GET "/api/datasources/uid/$DATASOURCE_UID"
case "$STATUS" in
  200)
    api datasource-put PUT "/api/datasources/uid/$DATASOURCE_UID" "$WORK/datasource.json"
    [[ "$STATUS" == 200 ]] || failed "The data source could not be updated" datasource-put
    info "Data source: $DATASOURCE_NAME ($DATASOURCE_UID) was there, updated."
    ;;
  404)
    api datasource-post POST /api/datasources "$WORK/datasource.json"
    [[ "$STATUS" == 200 ]] || failed "The data source could not be made" datasource-post
    info "Data source: $DATASOURCE_NAME ($DATASOURCE_UID) made."
    ;;
  *) failed "The data source could not be looked for" datasource ;;
esac

# The folder.
api folder GET "/api/folders/$FOLDER_UID"
case "$STATUS" in
  200) info "Folder: $FOLDER_TITLE is there." ;;
  404)
    python3 -c 'import json, sys; print(json.dumps({"uid": sys.argv[1], "title": sys.argv[2]}))' "$FOLDER_UID" "$FOLDER_TITLE" >"$WORK/folder-body.json"
    api folder-post POST /api/folders "$WORK/folder-body.json"
    [[ "$STATUS" == 200 ]] || failed "The folder could not be made" folder-post
    info "Folder: $FOLDER_TITLE made."
    ;;
  *) failed "The folder could not be looked for" folder ;;
esac

# The dashboard: made, or overwritten.
api dashboard-post POST /api/dashboards/db "$WORK/dashboard.json"
[[ "$STATUS" == 200 ]] || failed "The dashboard could not be saved" dashboard-post
info "Dashboard: $DASHBOARD_TITLE ($DASHBOARD_UID) saved in the folder $FOLDER_TITLE."

# What was added under the old name: removed now that the new ones are saved. The dashboard
# first (it uses the old data source); the folder last, and only when it is empty: Grafana would
# delete everything in it. A failure here is a WARNING: the new ones work all the same.
old_found=no old_failed=no
old_warning() {  # WHAT CALL_NAME
  old_failed=yes
  info "WARNING: $1: Grafana answered $STATUS: $(grafana_message "$WORK/answer-$2.json")"
}
old_remove() {  # WHAT CALL_NAME PATH
  old_found=yes
  api "$2-delete" DELETE "$3"
  if [[ "$STATUS" == 200 ]]; then info "Old name: $1 removed."; else old_warning "$1 could not be removed" "$2-delete"; fi
}
api old-dashboard GET "/api/dashboards/uid/$OLD_DASHBOARD_UID"
case "$STATUS" in
  200) old_remove "the dashboard $OLD_DASHBOARD_UID" old-dashboard "/api/dashboards/uid/$OLD_DASHBOARD_UID" ;;
  404) ;;
  *) old_warning "the old dashboard $OLD_DASHBOARD_UID could not be looked for" old-dashboard ;;
esac
api old-datasource GET "/api/datasources/uid/$OLD_DATASOURCE_UID"
case "$STATUS" in
  200) old_remove "the data source $OLD_DATASOURCE_UID" old-datasource "/api/datasources/uid/$OLD_DATASOURCE_UID" ;;
  404) ;;
  *) old_warning "the old data source $OLD_DATASOURCE_UID could not be looked for" old-datasource ;;
esac
api old-folder GET "/api/folders/$OLD_FOLDER_UID"
case "$STATUS" in
  200)
    api old-folder-search GET "/api/search?folderUIDs=$OLD_FOLDER_UID"
    if [[ "$STATUS" != 200 ]]; then
      old_warning "what is in the old folder $OLD_FOLDER_UID could not be read" old-folder-search
    elif python3 -c 'import json, sys; sys.exit(0 if json.load(open(sys.argv[1])) == [] else 1)' "$WORK/answer-old-folder-search.json" 2>/dev/null; then
      old_remove "the folder $OLD_FOLDER_UID (it was empty)" old-folder "/api/folders/$OLD_FOLDER_UID"
    else
      old_found=yes
      info "Old name: the folder $OLD_FOLDER_UID is kept: something else is in it. Move or delete that in Grafana, then delete the folder."
    fi
    ;;
  404) ;;
  *) old_warning "the old folder $OLD_FOLDER_UID could not be looked for" old-folder ;;
esac
if [[ "$old_found" == no && "$old_failed" == no ]]; then info "Old name: nothing of it is left in Grafana."; fi

# Grafana asks Tempo, from its own pod: this proves the network rule of Tempo. Not a stop: right
# after a deploy Tempo may still be starting.
api datasource-health GET "/api/datasources/uid/$DATASOURCE_UID/health"
if [[ "$STATUS" == 200 ]]; then
  info "Check: Grafana reaches Tempo ($(grafana_message "$WORK/answer-datasource-health.json"))."
else
  info "WARNING: Grafana cannot reach Tempo yet (status $STATUS: $(grafana_message "$WORK/answer-datasource-health.json"))."
  info "  Is Tempo running? bash k8s/doctor.sh. Then run this script again."
fi
grafana_stop

# Where to open it: the address of Grafana's Ingress, when it has one (only read).
hosts="$(grafana_k get ingress -o 'jsonpath={range .items[*]}{range .spec.rules[*]}{.host}{" "}{range .http.paths[*]}{.backend.service.name}{" "}{end}{"\n"}{end}{end}' 2>/dev/null \
  | awk -v service="$GRAFANA_SERVICE" '{ for (i = 2; i <= NF; i++) if ($i == service && $1 != "") { print $1; break } }' | sort -u || true)"
info ""
info "Done. Open the dashboard:"
if [[ -n "$hosts" ]]; then
  while IFS= read -r host; do info "  https://$host/d/$DASHBOARD_UID"; done <<<"$hosts"
  info "or, without the Ingress:"
fi
info "  kubectl --context $K8S_CONTEXT -n $GRAFANA_NAMESPACE port-forward service/$GRAFANA_SERVICE 3000:$(grafana_k get service "$GRAFANA_SERVICE" -o 'jsonpath={.spec.ports[0].port}' 2>/dev/null || echo 80)"
info "  then http://127.0.0.1:3000/d/$DASHBOARD_UID"
info "Explore (the trace of one task): Explore, data source \"$DATASOURCE_NAME\", TraceQL {name=\"wattson.task.end\" && span.wattson.task_id=\"<task id>\"}"
info "After Grafana restarts (its data is in an emptyDir), run this again: bash k8s/grafana.sh --apply"
if [[ "$old_failed" == yes ]]; then
  info "The old name is not removed from Grafana yet (the WARNING above): run this again."
  exit 1
fi
