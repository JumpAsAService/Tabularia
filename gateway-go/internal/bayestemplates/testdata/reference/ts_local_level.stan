// Local level model in its textbook centred form: a random-walk state observed with noise.
data {
  int<lower=2> N;
  vector[N] y;
}
parameters {
  vector[N] level;
  real<lower=0> sigma_level;
  real<lower=0> sigma_obs;
}
model {
  sigma_level ~ normal(0, 1);
  sigma_obs ~ exponential(1);
  level[1] ~ normal(0, 2.5);
  level[2:N] ~ normal(level[1:(N - 1)], sigma_level);
  y ~ normal(level, sigma_obs);
}
