"""The dbt export oracles against a running stack (see README.md).

usage: python3 run_all.py [quick|complete|browser|ai|all] [name …]
  quick     error codes, gateway and engine in step, dbt round trip, export security (~5 min)
  complete  quick + matrix, lifecycle, folder package, sample flows,
            determinism                                                  (~40 min, default)
  browser   the export dialog in Firefox (also its state between openings, focus, names)
  ai        descriptions and translation with the real AI (paid calls)
  all       complete + browser + ai
  name …    only those oracles (e.g. `python3 run_all.py matrix lifecycle`)
  cleanup   (Kubernetes) only removes the dbt pod and the oracles' NetworkPolicies

Before starting it checks the prerequisites: the API answers and the administrator signs
in, the sample databases and the engine's ClickHouse are there, the dbt image is there
(if missing it builds it), the case tables (schema.sql) are in the sample Postgres.
On Kubernetes (E2E_KUBE_CONTEXT) also: the image loaded on the dbt pod's node, the pod
running, the engine's ClickHouse reaching the source databases (the ClickHouse target's
federated databases; a NetworkPolicy of the oracles if it does not). At the end the pod and
those policies are removed (E2E_KUBE_KEEP=1 keeps them for the next round).
Each oracle writes its log in E2E_WORKDIR; at the end a summary, and it exits with an
error if an oracle is not green."""
import os, re, subprocess, sys, time

from environment import CH_CONTAINER, WORKDIR, IMAGE, PG_CONTAINER, HERE, BASE, KUBE, env
import common

SUITE = {
    "quick": ["codes", "drift", "round_trip", "security"],
    "complete": ["codes", "drift", "round_trip", "security", "matrix", "lifecycle", "folder_package", "sample_flows", "deterministic"],
    "browser": ["browser_dialog", "browser_dialog_state"],
    "ai": ["ai_descriptions", "ai_translation"],
}
# the two hunting oracles (their own zz… objects, a throwaway MariaDB on Kubernetes for native_targets' part D)
SUITE["hunt"] = ["clickhouse_paths", "native_targets"]
SUITE["all"] = SUITE["complete"] + SUITE["hunt"] + SUITE["browser"] + SUITE["ai"]


def prerequisites() -> list[str]:
    problems = []
    try:
        common.sign_in()
    except Exception as e:  # noqa: BLE001
        problems.append(f"the API {BASE} does not answer or the administrator cannot sign in ({e}): TABULARIA_URL, AUTH__ADMIN_EMAIL/PASSWORD")
    for name in (PG_CONTAINER, CH_CONTAINER):
        if not common.present(name):
            problems.append(f"the {'deployment' if KUBE else 'container'} «{name}» is not there (E2E_PG / E2E_CH_ENGINE)")
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0:
        print(f"the image {IMAGE} is missing: building it from Dockerfile.dbt…", flush=True)
        r = subprocess.run(["docker", "build", "-q", "-f", os.path.join(HERE, "Dockerfile.dbt"), "-t", IMAGE, HERE], capture_output=True, text=True)
        if r.returncode != 0:
            problems.append(f"the dbt image does not build: {r.stderr[-400:]}")
    if KUBE and not problems:
        problem = common.load_image()
        if problem:
            problems.append(problem)
        else:
            try:
                common.ensure_runner()
            except RuntimeError as e:
                problems.append(str(e))
    if KUBE and not problems:
        # the ClickHouse target: the engine's ClickHouse reads the sources live (federated databases)
        sources = {(c["host"], c.get("port") or 5432) for c in common.api("GET", "/connections")[1] if c["db_type"] == "postgresql" and c.get("host")}
        problem = common.reach_sources(sorted(sources))
        if problem:
            problems.append(problem)
    if not problems:
        r = common.sample_postgres(open(os.path.join(HERE, "schema.sql")).read())
        if r.returncode != 0:
            problems.append(f"the case tables cannot be created in the sample Postgres: {r.stderr[-300:]}")
    return problems


def launch(name: str) -> tuple[bool, str, float]:
    start = time.time()
    log = os.path.join(WORKDIR, f"{name}.log")
    with open(log, "w") as f:
        r = subprocess.run([sys.executable, os.path.join(HERE, f"{name}.py")], cwd=HERE, stdout=f, stderr=subprocess.STDOUT)
    text = open(log).read()
    count = re.findall(r"(\d+)/(\d+) checks passed", text)
    summary = f"{count[-1][0]}/{count[-1][1]}" if count else "no count"
    return r.returncode == 0, summary, time.time() - start


def main():
    arguments = sys.argv[1:] or ["complete"]
    if arguments == ["cleanup"]:
        if KUBE:
            common.kube_cleanup()
        return
    names = []
    for a in arguments:
        names += SUITE.get(a, [a])
    unknown = [n for n in names if not os.path.exists(os.path.join(HERE, f"{n}.py"))]
    if unknown:
        sys.exit(f"unknown oracles: {unknown}. Suites: {', '.join(SUITE)}")
    try:
        problems = prerequisites()
        if problems:
            print("missing prerequisites:\n  - " + "\n  - ".join(problems))
            sys.exit(2)
        outcomes = []
        for n in names:
            print(f"→ {n} …", end=" ", flush=True)
            green, summary, duration = launch(n)
            outcomes.append((n, green, summary))
            print(f"{'ok' if green else 'NO'}  {summary}  ({duration / 60:.1f} min)  log: {os.path.join(WORKDIR, n + '.log')}", flush=True)
    finally:
        if KUBE and not env("E2E_KUBE_KEEP"):
            common.kube_cleanup()
    red = [n for n, green, _ in outcomes if not green]
    print(f"\n{len(outcomes) - len(red)}/{len(outcomes)} oracles green" + (f" — to look at: {', '.join(red)}" if red else ""))
    sys.exit(1 if red else 0)


if __name__ == "__main__":
    main()
