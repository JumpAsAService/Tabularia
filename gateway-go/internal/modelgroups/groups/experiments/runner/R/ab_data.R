# Reading the data (docs/specs/ab-testing.md, «Types of the input's columns», «Reading the data: sufficient
# statistics»). Lot E0 sets the one thing the image's build is checked on; lot E1 writes the rest here: the
# reading, the unit collapse, the aggregated input, the cap, the covariate's empty values, the sums.
#
# arrow for R reads an Int64 whose values fit in 32 bits as an R integer (its option `arrow.int64_downcast`,
# true by default). This group keeps every input column's own type wherever one reaches an output (the
# variant of the `variants` output): an Int64 stays a 64-bit integer (`bit64::integer64`), so that the unit's
# id and the variant are never a double. The option is set when the file is sourced, before any reading.

options(arrow.int64_downcast = FALSE)
