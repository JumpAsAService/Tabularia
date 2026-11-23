// The self-test's check 4 (docs/specs/bayes-image.md, «The self-test»): the mean of a normal with a known standard deviation
// and a normal prior, whose posterior is normal in closed form: precision 1 / tau0^2 + N / sigma^2, mean
// (mu0 / tau0^2 + sum(y) / sigma^2) / precision.
data {
  int<lower=1> N;
  vector[N] y;
  real<lower=0> sigma;
  real mu0;
  real<lower=0> tau0;
}
parameters {
  real mu;
}
model {
  mu ~ normal(mu0, tau0);
  y ~ normal(mu, sigma);
}
