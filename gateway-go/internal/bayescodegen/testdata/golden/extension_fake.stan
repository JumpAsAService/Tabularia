data {
  int<lower=0> N;
  int<lower=1> J_grp;
  int<lower=1> K_c;
  array[N] int<lower=1, upper=K_c> c;
  array[N] int<lower=1, upper=J_grp> g;
  vector[N] x;
  vector[N] y;
  int<lower=0, upper=1> Mode;
  int<lower=0> Ppc_bins;
  vector[Ppc_bins] Ppc_edges;
}
parameters {
  vector[K_c - 1] b_c;
  real b_x;
  real mu;
  real<lower=0.0> s;
  real<lower=0.0> tau;
  vector<offset=mu, multiplier=tau>[J_grp] a;
}
model {
  vector[K_c] Full_bc = append_row(0.0, b_c);
  vector[N] eta = (((a[g]) + (b_x * x)) + (Full_bc[c]));
  b_c ~ normal(0.0, 1.0);
  b_x ~ normal(0.0, 1.0);
  mu ~ normal(0.0, 2.5);
  s ~ exponential(1.0);
  tau ~ normal(0.0, 1.0);
  a ~ normal(mu, tau);
  y ~ normal(eta, s);
}
generated quantities {
  real Ppc_first_rep;
  array[Mode == 1 ? J_grp : 0] real Prior_a;
  {
    vector[K_c] Full_bc = append_row(0.0, b_c);
    vector[N] eta = (((a[g]) + (b_x * x)) + (Full_bc[c]));
    real Prior_mu = normal_rng(0.0, 2.5);
    real Prior_tau = normal_rng(0.0, 1.0);
    for (I in 1:(Mode == 1 ? J_grp : 0)) {
      Prior_a[I] = normal_rng(Prior_mu, Prior_tau);
    }
    array[N] real Ppc_yrep = normal_rng(eta, s);
    Ppc_first_rep = Ppc_yrep[1];
  }
}
