data {
  int<lower=0> N;
  vector[N] y;
  int<lower=0> N_lag;
  array[N_lag] int<lower=1, upper=N> Rows_lag;
  array[N_lag] int<lower=1, upper=N> Lag1;
  int<lower=0, upper=1> Mode;
}
parameters {
  real delta;
  real<lower=0.0> sigma;
}
model {
  delta ~ normal(0.0, 300.0);
  sigma ~ exponential((1.0 / 300.0));
  y[Rows_lag] ~ normal(((y[Lag1]) + delta), sigma);
}
