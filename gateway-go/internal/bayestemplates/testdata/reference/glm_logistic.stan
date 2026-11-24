// Logistic regression, written by hand with the GLM form of Stan's functions library.
data {
  int<lower=0> N;
  matrix[N, 1] X;
  array[N] int<lower=0, upper=1> y;
}
parameters {
  real alpha;
  vector[1] beta;
}
model {
  alpha ~ normal(0, 2.5);
  beta ~ normal(0, 2.5);
  y ~ bernoulli_logit_glm(X, alpha, beta);
}
generated quantities {
  real b_x = beta[1];
}
