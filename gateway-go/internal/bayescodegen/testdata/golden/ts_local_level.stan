data {
  int<lower=0> N;
  vector[N] y;
  int<lower=0> N_first;
  array[N_first] int<lower=1, upper=N> Rows_first;
  int<lower=0> N_next;
  array[N_next] int<lower=1, upper=N> Rows_next;
  array[N_next] int<lower=1, upper=N> Prev;
  int<lower=0, upper=1> Mode;
}
parameters {
  real<lower=0.0> sigma_level;
  vector[N] Z_level;
  real<lower=0.0> sigma_obs;
}
transformed parameters {
  vector[N] level;
  level[Rows_first] = (0.0 + (2.5 * Z_level[Rows_first]));
  for (I in 1:N_next) {
    level[Rows_next[I]] = (level[Prev[I]] + (sigma_level * Z_level[Rows_next[I]]));
  }
}
model {
  sigma_level ~ normal(0.0, 1.0);
  Z_level ~ std_normal();
  sigma_obs ~ exponential(1.0);
  y ~ normal(level, sigma_obs);
}
