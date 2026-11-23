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
  real alpha;
  real<lower=0.0> sigma_level;
  real<lower=0.0> sigma_obs;
  real<lower=0.0> sigma_start;
  real<lower=0.0> sigma_trend;
  vector[N] trend;
  vector[N] Z_level;
}
transformed parameters {
  vector[N] level;
  {
    vector[N] spread = rep_vector((sigma_start * 2.0), N);
    vector[N] start = (alpha + (0.5 * trend));
    level[Rows_first] = (start[Rows_first] + (spread[Rows_first] .* Z_level[Rows_first]));
  }
  for (I in 1:N_next) {
    level[Rows_next[I]] = (level[Prev[I]] + (sigma_level * Z_level[Rows_next[I]]));
  }
}
model {
  vector[N] spread = rep_vector((sigma_start * 2.0), N);
  vector[N] start = (alpha + (0.5 * trend));
  alpha ~ normal(0.0, 2.5);
  sigma_level ~ exponential(1.0);
  sigma_obs ~ exponential(1.0);
  sigma_start ~ exponential(1.0);
  sigma_trend ~ exponential(1.0);
  trend[Rows_first] ~ normal(0.0, 10.0);
  trend[Rows_next] ~ normal(trend[Prev], sigma_trend);
  Z_level ~ std_normal();
  y ~ normal(level, sigma_obs);
}
