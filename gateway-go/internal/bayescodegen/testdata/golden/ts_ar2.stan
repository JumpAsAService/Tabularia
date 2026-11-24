data {
  int<lower=0> N;
  vector[N] y;
  int<lower=0> N_lag;
  array[N_lag] int<lower=1, upper=N> Rows_lag;
  array[N_lag] int<lower=1, upper=N> Lag1;
  array[N_lag] int<lower=1, upper=N> Lag2;
  int<lower=0, upper=1> Mode;
}
parameters {
  real mu;
  real phi1;
  real phi2;
  real<lower=0.0> sigma;
}
model {
  mu ~ normal(0.0, 2.5);
  phi1 ~ normal(0.0, 0.5);
  phi2 ~ normal(0.0, 0.5);
  sigma ~ exponential(1.0);
  y[Rows_lag] ~ normal(((mu + (phi1 * ((y[Lag1]) - mu))) + (phi2 * ((y[Lag2]) - mu))), sigma);
}
