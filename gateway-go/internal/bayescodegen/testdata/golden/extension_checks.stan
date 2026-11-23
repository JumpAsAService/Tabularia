data {
  int<lower=0> N;
  int<lower=1> J_grp;
  array[N] int<lower=1, upper=J_grp> g;
  vector[N] x;
  vector[N] y;
  int<lower=0, upper=1> Mode;
  int<lower=0> Ppc_on;
  int<lower=0> Ppc_o1_n;
  array[Ppc_o1_n] int<lower=0> Ppc_o1_rows;
  int<lower=0> Prior_o1_n;
  array[Prior_o1_n] int<lower=0> Prior_o1_rows;
  int<lower=0> Ppc_o1_m;
  int<lower=0> Ppc_o1_slots;
  real Ppc_o1_scale;
  real Ppc_o1_shift;
  int<lower=0> Ppc_o1_units;
  array[Ppc_o1_m] int<lower=0> Ppc_o1_slot;
  int<lower=0> Ppc_o1_binned;
  int<lower=0> Ppc_o1_rep;
}
parameters {
  real b_x;
  real mu_a;
  real<lower=0.0> sigma;
  real<lower=0.0> tau_a;
  vector<offset=mu_a, multiplier=tau_a>[J_grp] a_g;
}
model {
  vector[N] eta = ((a_g[g]) + (b_x * x));
  b_x ~ normal(0.0, (2.5 * 300.0));
  mu_a ~ normal(1200.0, (2.5 * 300.0));
  sigma ~ exponential((1.0 / 300.0));
  tau_a ~ normal(0.0, 300.0);
  a_g ~ normal(mu_a, tau_a);
  y ~ normal(eta, sigma);
}
generated quantities {
  real Prior_m_1;
  real Prior_m_3;
  real Prior_m_4;
  real Prior_m_5;
  vector[Mode == 1 ? J_grp : 0] Prior_p_6;
  array[Ppc_on] real Ppc_o1_mean;
  array[Ppc_on] real Ppc_o1_sd;
  array[Ppc_on] real Ppc_o1_min;
  array[Ppc_on] real Ppc_o1_max;
  array[Ppc_on] real Ppc_o1_low;
  array[Ppc_on] real Ppc_o1_median;
  array[Ppc_o1_slots] int Ppc_o1_count;
  vector[Ppc_o1_units] Ppc_o1_usum;
  array[Ppc_o1_rep] real Ppc_o1_yrep;
  {
    vector[N] eta = ((a_g[g]) + (b_x * x));
    Prior_m_1 = normal_rng(0.0, (2.5 * 300.0));
    Prior_m_3 = normal_rng(1200.0, (2.5 * 300.0));
    Prior_m_4 = exponential_rng((1.0 / 300.0));
    real Prior_u_5 = uniform_rng(Phi(((0.0 + ((-1.0) * 0.0)) * inv(300.0))), 1.0);
    Prior_m_5 = (0.0 + (300.0 * inv_Phi(Prior_u_5)));
    for (I in 1:(Mode == 1 ? J_grp : 0)) {
      Prior_p_6[I] = normal_rng(Prior_m_3, Prior_m_5);
    }
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(eta[Ppc_o1_rows], sigma);
    vector[N] E_eta_1;
    E_eta_1[Prior_o1_rows] = ((Prior_p_6[g[Prior_o1_rows]]) + (Prior_m_1 * x[Prior_o1_rows]));
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(E_eta_1[Prior_o1_rows], Prior_m_4);
    vector[Ppc_o1_m] Ppc_o1_y = to_vector(append_array(Ppc_o1_post, Prior_o1_draw));
    Ppc_o1_count = rep_array(0, Ppc_o1_slots);
    Ppc_o1_usum = rep_vector(0.0, Ppc_o1_units);
    for (I in 1:Ppc_on) {
      Ppc_o1_mean[I] = mean(Ppc_o1_y);
      Ppc_o1_sd[I] = sd(Ppc_o1_y);
      Ppc_o1_min[I] = min(Ppc_o1_y);
      Ppc_o1_max[I] = max(Ppc_o1_y);
      Ppc_o1_low[I] = quantile(Ppc_o1_y, 0.1);
      Ppc_o1_median[I] = quantile(Ppc_o1_y, 0.5);
    }
    for (I in 1:Ppc_o1_binned) {
      int Ppc_o1_b = to_int(fmin(fmax(floor(((Ppc_o1_y[I] * Ppc_o1_scale) + Ppc_o1_shift)), 1.0), Ppc_o1_slots));
      Ppc_o1_count[Ppc_o1_b] = (Ppc_o1_count[Ppc_o1_b] + 1);
      Ppc_o1_usum[Ppc_o1_slot[I]] = (Ppc_o1_usum[Ppc_o1_slot[I]] + Ppc_o1_y[I]);
    }
    for (I in 1:Ppc_o1_rep) {
      Ppc_o1_yrep[I] = Ppc_o1_y[I];
    }
  }
}
