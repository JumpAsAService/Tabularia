// Varying intercepts, the textbook non-centred form: a_g = mu_a + tau_a * z_g.
data {
  int<lower=0> N;
  int<lower=1> J;
  array[N] int<lower=1, upper=J> g;
  vector[N] x;
  vector[N] y;
}
parameters {
  real mu_a;
  real b_x;
  real<lower=0> tau_a;
  real<lower=0> sigma;
  vector[J] z_a;
}
transformed parameters {
  vector[J] a_g = mu_a + tau_a * z_a;
}
model {
  mu_a ~ normal(0, 2.5);
  b_x ~ normal(0, 2.5);
  tau_a ~ normal(0, 1);
  sigma ~ exponential(1);
  z_a ~ std_normal();
  for (n in 1:N)
    y[n] ~ normal(a_g[g[n]] + b_x * x[n], sigma);
}
