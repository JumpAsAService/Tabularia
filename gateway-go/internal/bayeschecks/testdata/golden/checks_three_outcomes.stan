data {
  int<lower=0> N;
  int<lower=1> J_grp;
  array[N] int<lower=1, upper=J_grp> g;
  array[N] int<lower=0> k;
  vector[N] x;
  vector[N] y;
  array[N] int<lower=0, upper=1> z;
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
  int<lower=0> Ppc_o1_binned;
  int<lower=0> Ppc_o1_units;
  array[Ppc_o1_binned] int<lower=0> Ppc_o1_slot;
  int<lower=0> Ppc_o1_rep;
  int<lower=0> Ppc_o2_n;
  array[Ppc_o2_n] int<lower=0> Ppc_o2_rows;
  int<lower=0> Prior_o2_n;
  array[Prior_o2_n] int<lower=0> Prior_o2_rows;
  int<lower=0> Ppc_o2_m;
  int<lower=0> Ppc_o2_slots;
  real Ppc_o2_scale;
  real Ppc_o2_shift;
  int<lower=0> Ppc_o2_binned;
  int<lower=0> Ppc_o2_units;
  array[Ppc_o2_binned] int<lower=0> Ppc_o2_slot;
  int<lower=0> Ppc_o2_rep;
  int<lower=0> Ppc_o3_n;
  array[Ppc_o3_n] int<lower=0> Ppc_o3_rows;
  int<lower=0> Prior_o3_n;
  array[Prior_o3_n] int<lower=0> Prior_o3_rows;
  int<lower=0> Ppc_o3_m;
  int<lower=0> Ppc_o3_slots;
  real Ppc_o3_scale;
  real Ppc_o3_shift;
  int<lower=0> Ppc_o3_binned;
  int<lower=0> Ppc_o3_rep;
}
parameters {
  real b;
  real mu;
  real<lower=0.0> s;
  real<lower=0.0> tau;
  vector<offset=mu, multiplier=tau>[J_grp] a_g;
}
model {
  b ~ normal(0.0, 1.0);
  mu ~ normal(0.0, 1.0);
  s ~ exponential(1.0);
  tau ~ exponential(1.0);
  a_g ~ normal(mu, tau);
  k ~ poisson_log((a_g[g]));
  y ~ normal(((a_g[g]) + (b * x)), s);
  z ~ bernoulli_logit((b * x));
}
generated quantities {
  real Prior_m_1;
  real Prior_d_1;
  real Prior_m_4;
  real Prior_d_4;
  real Prior_m_5;
  real Prior_d_5;
  real Prior_m_6;
  real Prior_d_6;
  real Prior_m_7;
  vector[Mode == 1 ? J_grp : 0] Prior_p_7;
  array[Ppc_on] real Ppc_o1_mean;
  array[Ppc_on] real Ppc_o1_sd;
  array[Ppc_on] real Ppc_o1_max;
  array[Ppc_on] real Ppc_o1_q50;
  array[Ppc_on] real Ppc_o1_q90;
  array[Ppc_on] real Ppc_o1_zeros;
  array[Ppc_o1_slots] int Ppc_o1_count;
  vector[Ppc_o1_units] Ppc_o1_usum;
  array[Ppc_o1_rep] real Ppc_o1_yrep;
  array[Ppc_on] real Ppc_o2_mean;
  array[Ppc_on] real Ppc_o2_sd;
  array[Ppc_on] real Ppc_o2_min;
  array[Ppc_on] real Ppc_o2_max;
  array[Ppc_on] real Ppc_o2_q10;
  array[Ppc_on] real Ppc_o2_q50;
  array[Ppc_on] real Ppc_o2_q90;
  array[Ppc_o2_slots] int Ppc_o2_count;
  vector[Ppc_o2_units] Ppc_o2_usum;
  array[Ppc_o2_rep] real Ppc_o2_yrep;
  array[Ppc_on] real Ppc_o3_mean;
  array[Ppc_o3_slots] int Ppc_o3_count;
  array[Ppc_o3_rep] real Ppc_o3_yrep;
  {
    Prior_m_1 = normal_rng(0.0, 1.0);
    Prior_d_1 = Prior_m_1;
    Prior_m_4 = normal_rng(0.0, 1.0);
    Prior_d_4 = Prior_m_4;
    Prior_m_5 = exponential_rng(1.0);
    Prior_d_5 = Prior_m_5;
    Prior_m_6 = exponential_rng(1.0);
    Prior_d_6 = Prior_m_6;
    real Prior_g_m7_1 = Prior_m_4;
    real Prior_g_m7_2 = Prior_m_6;
    Prior_m_7 = (normal_rng(fmin(fmax(Prior_g_m7_1, (-1e+300)), 1e+300), fmin(fmax(Prior_g_m7_2, 1e-300), 1e+300)) + (0.0 * (Prior_g_m7_1 + Prior_g_m7_2)));
    real Prior_a_p7_1 = Prior_m_4;
    real Prior_a_p7_2 = Prior_m_6;
    for (I in 1:(Mode == 1 ? J_grp : 0)) {
      Prior_p_7[I] = (normal_rng(fmin(fmax(Prior_a_p7_1, (-1e+300)), 1e+300), fmin(fmax(Prior_a_p7_2, 1e-300), 1e+300)) + (0.0 * (Prior_a_p7_1 + Prior_a_p7_2)));
    }
    array[Ppc_o1_n] int Ppc_o1_post = poisson_log_rng((a_g[g[Ppc_o1_rows]]));
    array[Prior_o1_n] int Prior_o1_draw = poisson_log_rng((Prior_p_7[g[Prior_o1_rows]]));
    vector[Ppc_o1_m] Ppc_o1_y = to_vector(append_array(Ppc_o1_post, Prior_o1_draw));
    Ppc_o1_count = rep_array(0, Ppc_o1_slots);
    Ppc_o1_usum = rep_vector(0.0, Ppc_o1_units);
    for (I in 1:Ppc_on) {
      Ppc_o1_mean[I] = mean(Ppc_o1_y);
      Ppc_o1_sd[I] = sd(Ppc_o1_y);
      Ppc_o1_max[I] = max(Ppc_o1_y);
      Ppc_o1_q50[I] = quantile(Ppc_o1_y, 0.5);
      Ppc_o1_q90[I] = quantile(Ppc_o1_y, 0.9);
      Ppc_o1_zeros[I] = 0.0;
    }
    for (I in 1:Ppc_o1_binned) {
      int Ppc_o1_b = to_int(fmin(fmax(floor(((Ppc_o1_y[I] * Ppc_o1_scale) + Ppc_o1_shift)), 1.0), Ppc_o1_slots));
      Ppc_o1_count[Ppc_o1_b] = (Ppc_o1_count[Ppc_o1_b] + 1);
      Ppc_o1_usum[Ppc_o1_slot[I]] = (Ppc_o1_usum[Ppc_o1_slot[I]] + Ppc_o1_y[I]);
      Ppc_o1_zeros[1] = (Ppc_o1_zeros[1] + (1.0 + ((-1.0) * fmin(Ppc_o1_y[I], 1.0))));
    }
    for (I in 1:Ppc_o1_rep) {
      Ppc_o1_yrep[I] = Ppc_o1_y[I];
    }
    array[Ppc_o2_n] real Ppc_o2_post = normal_rng(((a_g[g[Ppc_o2_rows]]) + (b * x[Ppc_o2_rows])), s);
    array[Prior_o2_n] real Prior_o2_draw = normal_rng(((Prior_p_7[g[Prior_o2_rows]]) + (Prior_m_1 * x[Prior_o2_rows])), Prior_m_5);
    vector[Ppc_o2_m] Ppc_o2_y = to_vector(append_array(Ppc_o2_post, Prior_o2_draw));
    Ppc_o2_count = rep_array(0, Ppc_o2_slots);
    Ppc_o2_usum = rep_vector(0.0, Ppc_o2_units);
    for (I in 1:Ppc_on) {
      Ppc_o2_mean[I] = mean(Ppc_o2_y);
      Ppc_o2_sd[I] = sd(Ppc_o2_y);
      Ppc_o2_min[I] = min(Ppc_o2_y);
      Ppc_o2_max[I] = max(Ppc_o2_y);
      Ppc_o2_q10[I] = quantile(Ppc_o2_y, 0.1);
      Ppc_o2_q50[I] = quantile(Ppc_o2_y, 0.5);
      Ppc_o2_q90[I] = quantile(Ppc_o2_y, 0.9);
    }
    for (I in 1:Ppc_o2_binned) {
      int Ppc_o2_b = to_int(fmin(fmax(floor(((Ppc_o2_y[I] * Ppc_o2_scale) + Ppc_o2_shift)), 1.0), Ppc_o2_slots));
      Ppc_o2_count[Ppc_o2_b] = (Ppc_o2_count[Ppc_o2_b] + 1);
      Ppc_o2_usum[Ppc_o2_slot[I]] = (Ppc_o2_usum[Ppc_o2_slot[I]] + Ppc_o2_y[I]);
    }
    for (I in 1:Ppc_o2_rep) {
      Ppc_o2_yrep[I] = Ppc_o2_y[I];
    }
    array[Ppc_o3_n] int Ppc_o3_post = bernoulli_logit_rng((b * x[Ppc_o3_rows]));
    array[Prior_o3_n] int Prior_o3_draw = bernoulli_logit_rng((Prior_m_1 * x[Prior_o3_rows]));
    vector[Ppc_o3_m] Ppc_o3_y = to_vector(append_array(Ppc_o3_post, Prior_o3_draw));
    Ppc_o3_count = rep_array(0, Ppc_o3_slots);
    for (I in 1:Ppc_on) {
      Ppc_o3_mean[I] = mean(Ppc_o3_y);
    }
    for (I in 1:Ppc_o3_binned) {
      int Ppc_o3_b = to_int(fmin(fmax(floor(((Ppc_o3_y[I] * Ppc_o3_scale) + Ppc_o3_shift)), 1.0), Ppc_o3_slots));
      Ppc_o3_count[Ppc_o3_b] = (Ppc_o3_count[Ppc_o3_b] + 1);
    }
    for (I in 1:Ppc_o3_rep) {
      Ppc_o3_yrep[I] = Ppc_o3_y[I];
    }
  }
}
