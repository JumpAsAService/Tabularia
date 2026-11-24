// Negative binomial regression (mean and dispersion, log link), written by hand with the natural-scale
// parameterization: y ~ NegBinomial2(exp(alpha + beta * x), phi).
data {
  int<lower=0> N;
  matrix[N, 1] X;
  array[N] int<lower=0> y;
}
parameters {
  real alpha;
  vector[1] beta;
  real<lower=0> phi;
}
transformed parameters {
  real b_x = beta[1];
}
model {
  alpha ~ normal(0, 2.5);
  beta ~ normal(0, 2.5);
  phi ~ inv_gamma(0.4, 0.3);
  y ~ neg_binomial_2(exp(alpha + X * beta), phi);
}
