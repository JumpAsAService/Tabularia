data {
  int<lower=0> N;
  vector[N] y;
  int<lower=0> N_first;
  array[N_first] int<lower=1, upper=N> Rows_first;
  int<lower=0> N_next;
  array[N_next] int<lower=1, upper=N> Rows_next;
  array[N_next] int<lower=1, upper=N> Prev;
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
  int<lower=0> Ppc_o1_rep;
  int<lower=0> Prior_first_n;
  array[Prior_first_n] int<lower=0> Prior_first;
  int<lower=0> Prior_next_n;
  array[Prior_next_n] int<lower=0> Prior_next;
  array[Prior_next_n] int<lower=0> Prior_prev;
}
parameters {
  real<lower=0.0> sigma_level;
  vector[N] level;
  real<lower=0.0> sigma_obs;
}
model {
  sigma_level ~ normal(0.0, 300.0);
  level[Rows_first] ~ normal(1200.0, (2.5 * 300.0));
  level[Rows_next] ~ normal(level[Prev], sigma_level);
  sigma_obs ~ exponential((1.0 / 300.0));
  y ~ normal(level, sigma_obs);
}
generated quantities {
  real Prior_m_1;
  real Prior_d_1;
  real Prior_m_2;
  vector[Mode == 1 ? N : 0] Prior_p_2;
  real Prior_m_3;
  real Prior_d_3;
  array[Ppc_on] real Ppc_o1_mean;
  array[Ppc_on] real Ppc_o1_sd;
  array[Ppc_on] real Ppc_o1_min;
  array[Ppc_on] real Ppc_o1_max;
  array[Ppc_on] real Ppc_o1_q10;
  array[Ppc_on] real Ppc_o1_q50;
  array[Ppc_on] real Ppc_o1_q90;
  array[Ppc_o1_slots] int Ppc_o1_count;
  array[Ppc_o1_rep] real Ppc_o1_yrep;
  {
    real Prior_u_m1 = (Phi(((0.0 + ((-1.0) * 0.0)) * inv(300.0))) + ((1.0 + ((-1.0) * Phi(((0.0 + ((-1.0) * 0.0)) * inv(300.0))))) * uniform_rng(0.0, 1.0)));
    Prior_m_1 = (0.0 + (300.0 * inv_Phi(fmin(fmax(Prior_u_m1, 0.0), 1.0))));
    Prior_d_1 = Prior_m_1;
    real Prior_g_m2_2 = (2.5 * 300.0);
    Prior_m_2 = (normal_rng(1200.0, fmin(fmax(Prior_g_m2_2, 1e-300), 1e+300)) + (0.0 * Prior_g_m2_2));
    real Prior_a_p2_1 = 1200.0;
    real Prior_a_p2_2 = (2.5 * 300.0);
    real Prior_a_p2_3 = Prior_m_1;
    for (I in 1:Prior_first_n) {
      Prior_p_2[Prior_first[I]] = (normal_rng(Prior_a_p2_1, fmin(fmax(Prior_a_p2_2, 1e-300), 1e+300)) + (0.0 * Prior_a_p2_2));
    }
    for (I in 1:Prior_next_n) {
      Prior_p_2[Prior_next[I]] = (normal_rng(fmin(fmax(Prior_p_2[Prior_prev[I]], (-1e+300)), 1e+300), fmin(fmax(Prior_a_p2_3, 1e-300), 1e+300)) + (0.0 * (Prior_p_2[Prior_prev[I]] + Prior_a_p2_3)));
    }
    real Prior_g_m3_1 = (1.0 / 300.0);
    Prior_m_3 = (exponential_rng(fmin(fmax(Prior_g_m3_1, 1e-300), 1e+300)) + (0.0 * Prior_g_m3_1));
    Prior_d_3 = Prior_m_3;
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(level[Ppc_o1_rows], sigma_obs);
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(Prior_p_2[Prior_o1_rows], Prior_m_3);
    vector[Ppc_o1_m] Ppc_o1_y = to_vector(append_array(Ppc_o1_post, Prior_o1_draw));
    Ppc_o1_count = rep_array(0, Ppc_o1_slots);
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
    }
    for (I in 1:Ppc_o1_rep) {
      Ppc_o1_yrep[I] = Ppc_o1_y[I];
    }
  }
}
