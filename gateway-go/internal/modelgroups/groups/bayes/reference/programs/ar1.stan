// The self-test's AR(1) (docs/specs/bayes-image.md, «Compiling three typical programs» and the self-test's checks 2 and 3):
// compiled as the contract says, then once more with -H to see the precompiled header used; never sampled here.
data {
  int<lower=2> N;
  vector[N] y;
}
parameters {
  real alpha;
  real<lower=-1, upper=1> beta;
  real<lower=0> sigma;
}
model {
  alpha ~ normal(0, 5);
  beta ~ normal(0, 0.5);
  sigma ~ exponential(1);
  y[2:N] ~ normal(alpha + beta * y[1:(N - 1)], sigma);
}
