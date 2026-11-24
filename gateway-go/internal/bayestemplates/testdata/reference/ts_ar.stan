// AR(2) around a mean, the textbook conditional likelihood: the first two points are not modelled.
data {
  int<lower=3> N;
  vector[N] y;
}
parameters {
  real mu;
  real phi1;
  real phi2;
  real<lower=0> sigma;
}
model {
  mu ~ normal(0, 2.5);
  phi1 ~ normal(0, 0.5);
  phi2 ~ normal(0, 0.5);
  sigma ~ exponential(1);
  for (t in 3:N)
    y[t] ~ normal(mu + phi1 * (y[t - 1] - mu) + phi2 * (y[t - 2] - mu), sigma);
}
