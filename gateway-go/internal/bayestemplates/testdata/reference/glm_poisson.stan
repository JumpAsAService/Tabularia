// Poisson regression with a log link, written by hand with the GLM form.
data {
  int<lower=0> N;
  matrix[N, 1] X;
  array[N] int<lower=0> y;
}
parameters {
  real alpha;
  vector[1] beta;
}
model {
  alpha ~ normal(0, 2.5);
  beta ~ normal(0, 2.5);
  y ~ poisson_log_glm(X, alpha, beta);
}
generated quantities {
  real b_x = beta[1];
}
