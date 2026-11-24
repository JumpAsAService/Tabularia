// Linear regression, written by hand with the GLM form of Stan's functions library.
data {
  int<lower=0> N;
  matrix[N, 1] X;   // the standardized predictor
  vector[N] y;
}
parameters {
  real alpha;
  vector[1] beta;
  real<lower=0> sigma;
}
model {
  alpha ~ normal(0, 2.5);
  beta ~ normal(0, 2.5);
  sigma ~ exponential(1);
  y ~ normal_id_glm(X, alpha, beta, sigma);
}
generated quantities {
  real b_x = beta[1];
}
