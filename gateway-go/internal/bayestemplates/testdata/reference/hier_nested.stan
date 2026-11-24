// Two nested levels (schools within districts), non-centred: each level is its parent plus tau * z.
data {
  int<lower=0> N;
  int<lower=1> J_d;
  int<lower=1> J_s;
  array[N] int<lower=1, upper=J_s> s;
  array[J_s] int<lower=1, upper=J_d> district_of;
  vector[N] x;
  vector[N] y;
}
parameters {
  real mu;
  real b_x;
  real<lower=0> tau_d;
  real<lower=0> tau_s;
  real<lower=0> sigma;
  vector[J_d] z_d;
  vector[J_s] z_s;
}
transformed parameters {
  vector[J_d] u_d = mu + tau_d * z_d;
  vector[J_s] u_s = u_d[district_of] + tau_s * z_s;
}
model {
  mu ~ normal(0, 2.5);
  b_x ~ normal(0, 2.5);
  tau_d ~ normal(0, 1);
  tau_s ~ normal(0, 1);
  sigma ~ exponential(1);
  z_d ~ std_normal();
  z_s ~ std_normal();
  for (n in 1:N)
    y[n] ~ normal(u_s[s[n]] + b_x * x[n], sigma);
}
