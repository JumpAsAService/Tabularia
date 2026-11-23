data {
  int<lower=0> N;
  int<lower=1> J_dst;
  int<lower=1> J_sch;
  array[N] int<lower=1, upper=J_dst> d;
  array[N] int<lower=1, upper=J_sch> s;
  vector[N] x;
  vector[N] y;
  array[J_sch] int<lower=1, upper=J_dst> Up_d_sch;
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
}
parameters {
  real b_x;
  real mu;
  real<lower=0.0> sigma;
  real<lower=0.0> tau_d;
  real<lower=0.0> tau_s;
  vector<offset=mu, multiplier=tau_d>[J_dst] u_d;
  vector<offset=(u_d[Up_d_sch]), multiplier=tau_s>[J_sch] u_s;
}
model {
  vector[N] eta = ((u_s[s]) + (b_x * x));
  b_x ~ normal(0.0, (2.5 * 300.0));
  mu ~ normal(1200.0, (2.5 * 300.0));
  sigma ~ exponential((1.0 / 300.0));
  tau_d ~ normal(0.0, 300.0);
  tau_s ~ normal(0.0, 300.0);
  u_d ~ normal(mu, tau_d);
  u_s ~ normal((u_d[Up_d_sch]), tau_s);
  y ~ normal(eta, sigma);
}
generated quantities {
  real Prior_m_1;
  real Prior_d_1;
  real Prior_m_3;
  real Prior_d_3;
  real Prior_m_5;
  real Prior_d_5;
  real Prior_m_6;
  real Prior_d_6;
  real Prior_m_7;
  real Prior_d_7;
  real Prior_m_8;
  vector[Mode == 1 ? J_dst : 0] Prior_p_8;
  real Prior_m_9;
  vector[Mode == 1 ? J_sch : 0] Prior_p_9;
  array[Ppc_on] real Ppc_o1_mean;
  array[Ppc_on] real Ppc_o1_sd;
  array[Ppc_on] real Ppc_o1_min;
  array[Ppc_on] real Ppc_o1_max;
  array[Ppc_on] real Ppc_o1_q10;
  array[Ppc_on] real Ppc_o1_q50;
  array[Ppc_on] real Ppc_o1_q90;
  array[Ppc_o1_slots] int Ppc_o1_count;
  vector[Ppc_o1_units] Ppc_o1_usum;
  array[Ppc_o1_rep] real Ppc_o1_yrep;
  {
    vector[N] eta = ((u_s[s]) + (b_x * x));
    real Prior_g_m1_2 = (2.5 * 300.0);
    Prior_m_1 = (normal_rng(0.0, fmin(fmax(Prior_g_m1_2, 1e-300), 1e+300)) + (0.0 * Prior_g_m1_2));
    Prior_d_1 = Prior_m_1;
    real Prior_g_m3_2 = (2.5 * 300.0);
    Prior_m_3 = (normal_rng(1200.0, fmin(fmax(Prior_g_m3_2, 1e-300), 1e+300)) + (0.0 * Prior_g_m3_2));
    Prior_d_3 = Prior_m_3;
    real Prior_g_m5_1 = (1.0 / 300.0);
    Prior_m_5 = (exponential_rng(fmin(fmax(Prior_g_m5_1, 1e-300), 1e+300)) + (0.0 * Prior_g_m5_1));
    Prior_d_5 = Prior_m_5;
    real Prior_u_m6 = (Phi(((0.0 + ((-1.0) * 0.0)) * inv(300.0))) + ((1.0 + ((-1.0) * Phi(((0.0 + ((-1.0) * 0.0)) * inv(300.0))))) * uniform_rng(0.0, 1.0)));
    Prior_m_6 = (0.0 + (300.0 * inv_Phi(fmin(fmax(Prior_u_m6, 0.0), 1.0))));
    Prior_d_6 = Prior_m_6;
    real Prior_u_m7 = (Phi(((0.0 + ((-1.0) * 0.0)) * inv(300.0))) + ((1.0 + ((-1.0) * Phi(((0.0 + ((-1.0) * 0.0)) * inv(300.0))))) * uniform_rng(0.0, 1.0)));
    Prior_m_7 = (0.0 + (300.0 * inv_Phi(fmin(fmax(Prior_u_m7, 0.0), 1.0))));
    Prior_d_7 = Prior_m_7;
    real Prior_g_m8_1 = Prior_m_3;
    real Prior_g_m8_2 = Prior_m_6;
    Prior_m_8 = (normal_rng(fmin(fmax(Prior_g_m8_1, (-1e+300)), 1e+300), fmin(fmax(Prior_g_m8_2, 1e-300), 1e+300)) + (0.0 * (Prior_g_m8_1 + Prior_g_m8_2)));
    real Prior_a_p8_1 = Prior_m_3;
    real Prior_a_p8_2 = Prior_m_6;
    for (I in 1:(Mode == 1 ? J_dst : 0)) {
      Prior_p_8[I] = (normal_rng(fmin(fmax(Prior_a_p8_1, (-1e+300)), 1e+300), fmin(fmax(Prior_a_p8_2, 1e-300), 1e+300)) + (0.0 * (Prior_a_p8_1 + Prior_a_p8_2)));
    }
    vector[J_dst] Prior_r_8;
    for (I in 1:J_dst) {
      Prior_r_8[I] = Prior_m_8;
    }
    vector[J_sch] Prior_v_9_1 = (Prior_r_8[Up_d_sch]);
    real Prior_g_m9_1 = Prior_v_9_1[1];
    real Prior_g_m9_2 = Prior_m_7;
    Prior_m_9 = (normal_rng(fmin(fmax(Prior_g_m9_1, (-1e+300)), 1e+300), fmin(fmax(Prior_g_m9_2, 1e-300), 1e+300)) + (0.0 * (Prior_g_m9_1 + Prior_g_m9_2)));
    vector[Mode == 1 ? J_sch : 0] Prior_a_p9_1;
    real Prior_a_p9_2 = Prior_m_7;
    for (I in 1:(Mode == 1 ? 1 : 0)) {
      Prior_a_p9_1 = (Prior_p_8[Up_d_sch]);
    }
    for (I in 1:(Mode == 1 ? J_sch : 0)) {
      Prior_p_9[I] = (normal_rng(fmin(fmax(Prior_a_p9_1[I], (-1e+300)), 1e+300), fmin(fmax(Prior_a_p9_2, 1e-300), 1e+300)) + (0.0 * (Prior_a_p9_1[I] + Prior_a_p9_2)));
    }
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(eta[Ppc_o1_rows], sigma);
    vector[N] E_eta_1;
    E_eta_1[Prior_o1_rows] = ((Prior_p_9[s[Prior_o1_rows]]) + (Prior_m_1 * x[Prior_o1_rows]));
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(E_eta_1[Prior_o1_rows], Prior_m_5);
    vector[Ppc_o1_m] Ppc_o1_y = to_vector(append_array(Ppc_o1_post, Prior_o1_draw));
    Ppc_o1_count = rep_array(0, Ppc_o1_slots);
    Ppc_o1_usum = rep_vector(0.0, Ppc_o1_units);
    for (I in 1:Ppc_on) {
      Ppc_o1_mean[I] = mean(Ppc_o1_y);
      Ppc_o1_sd[I] = sd(Ppc_o1_y);
      Ppc_o1_min[I] = min(Ppc_o1_y);
      Ppc_o1_max[I] = max(Ppc_o1_y);
      Ppc_o1_q10[I] = quantile(Ppc_o1_y, 0.1);
      Ppc_o1_q50[I] = quantile(Ppc_o1_y, 0.5);
      Ppc_o1_q90[I] = quantile(Ppc_o1_y, 0.9);
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
