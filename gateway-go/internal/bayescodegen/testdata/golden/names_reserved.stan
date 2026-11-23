data {
  int<lower=0> N;
  vector[N] V_x;
  vector[N] V_y;
  int<lower=0, upper=1> Mode;
}
parameters {
  real V_a;
  real V_b;
  real<lower=0.0> sigma;
}
model {
  vector[N] eta = (V_a + (V_b * V_x));
  V_a ~ normal(0.0, 2.5);
  V_b ~ normal(0.0, 2.5);
  sigma ~ exponential(1.0);
  V_y ~ normal(eta, sigma);
}
