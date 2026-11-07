#!/bin/sh
# The licences of what the data APIs' program links, for its image (dataapi/Dockerfile): every
# module the build used (go list -deps, for this platform) must be in licences.json, reviewed by
# hand, and carry its licence file; the files are copied to <out>/<module>/, and <out>/modules.json
# lists the modules and their licences for the release's check (.github/scripts/image_licence_check.py
# --kind go). Run in dataapi/ after the build:  sh licences.sh /out/licenses
set -eu
out=$1
mkdir -p "$out"
list=$(mktemp)
go list -deps -f '{{with .Module}}{{.Path}} {{.Version}} {{.Dir}}{{end}}' . | sort -u > "$list"
# the Go runtime and standard library are linked too
echo "std $(go env GOVERSION) $(go env GOROOT)" >> "$list"
printf '[' > "$out/modules.json"
sep=''
while read -r path version dir; do
    case "$path" in github.com/JumpAsAService/Tabularia/*) continue ;; esac
    licence=$(grep -F "\"$path\":" licences.json | sed 's/.*": *"\([^"]*\)".*/\1/' || true)
    if [ -z "$licence" ]; then
        echo "licences: $path $version is not in licences.json: review its licence and add it" >&2
        exit 1
    fi
    files=$(ls "$dir" | grep -iE '^(licen[cs]e|copying|notice|patents)' || true)
    if [ -z "$files" ]; then
        echo "licences: $path $version has no licence file" >&2
        exit 1
    fi
    mkdir -p "$out/$path"
    for f in $files; do cp "$dir/$f" "$out/$path/"; done
    printf '%s{"kind": "go", "name": "%s", "version": "%s", "license": "%s"}' "$sep" "$path" "$version" "$licence" >> "$out/modules.json"
    sep=', '
done < "$list"
printf ']\n' >> "$out/modules.json"
rm -f "$list"
