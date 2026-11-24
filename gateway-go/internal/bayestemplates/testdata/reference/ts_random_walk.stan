// Random walk with drift: the first point is not modelled.
data {
  int<lower=2> N;
  vector[N] y;
}
parameters {
  real delta;
  real<lower=0> sigma;
}
model {
  delta ~ normal(0, 1);
  sigma ~ exponential(1);
  for (t in 2:N)
    y[t] ~ normal(y[t - 1] + delta, sigma);
}
