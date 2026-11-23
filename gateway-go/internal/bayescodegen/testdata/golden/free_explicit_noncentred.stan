data {
  int<lower=0> N;
  int<lower=1> J_grp;
  array[N] int<lower=1, upper=J_grp> g;
  vector[N] y;
  int<lower=0, upper=1> Mode;
}
parameters {
  real log_m;
  real<lower=0.0> sigma;
  real<lower=0.0> tau;
  vector[J_grp] Z_ag;
}
transformed parameters {
  real m = exp(log_m);
  vector[J_grp] a_g = (m + (tau * Z_ag));
}
model {
  log_m ~ normal(0.0, 1.0);
  sigma ~ exponential(1.0);
  tau ~ normal(0.0, 1.0);
  Z_ag ~ std_normal();
  y ~ normal((a_g[g]), sigma);
}
