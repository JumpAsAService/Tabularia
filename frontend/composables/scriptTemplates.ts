// The code a new Script node starts from, in its environment's language: it reads
// its input and writes its output when the node has them (TABULARIA_INPUT and
// TABULARIA_OUTPUT are empty when it does not).
export function starterFiles(language: 'python' | 'r'): { path: string; content: string }[] {
  if (language === 'r') {
    return [{
      path: 'main.R',
      content: `library(arrow)

# The step before this node, when an edge enters it; empty otherwise
source_uri <- Sys.getenv("TABULARIA_INPUT")
# Where the nodes after this one read from, when an edge leaves it; empty otherwise
target_uri <- Sys.getenv("TABULARIA_OUTPUT")

if (nzchar(source_uri)) {
  df <- read_parquet(source_uri)

  # your code here

  if (nzchar(target_uri)) write_parquet(df, target_uri)
}
`,
    }]
  }
  return [{
    path: 'main.py',
    content: `import os

import polars as pl

# The step before this node, when an edge enters it; empty otherwise
source = os.environ.get("TABULARIA_INPUT")
# Where the nodes after this one read from, when an edge leaves it; empty otherwise
target = os.environ.get("TABULARIA_OUTPUT")

if source:
    df = pl.scan_parquet(source)

    # your code here

    if target:
        df.sink_parquet(target)
`,
  }]
}
