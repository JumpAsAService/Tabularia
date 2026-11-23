data {
  int<lower=0> N;
  int<lower=1> K_ch;
  array[N] int<lower=1, upper=K_ch> channel;
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
  int<lower=0> Ppc_o1_binned;
  int<lower=0> Ppc_o1_rep;
}
parameters {
  real alpha;
  vector[K_ch - 1] b_channel;
  real b_x;
  real<lower=0.0> sigma;
}
model {
  vector[K_ch] Full_bch = append_row(0.0, b_channel);
  vector[N] eta = ((alpha + (b_x * x)) + (Full_bch[channel]));
  alpha ~ normal(0.0, 2.5);
  b_channel ~ normal(0.0, 1.0);
  b_x ~ normal(0.0, 1.0);
  sigma ~ exponential(1.0);
  y ~ normal(eta, sigma);
}
generated quantities {
  real Prior_m_1;
  real Prior_d_1;
  real Prior_m_2;
  vector[Mode == 1 ? K_ch - 1 : 0] Prior_p_2;
  real Prior_m_3;
  real Prior_d_3;
  real Prior_m_5;
  real Prior_d_5;
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
    vector[K_ch] Full_bch = append_row(0.0, b_channel);
    vector[N] eta = ((alpha + (b_x * x)) + (Full_bch[channel]));
    Prior_m_1 = normal_rng(0.0, 2.5);
    Prior_d_1 = Prior_m_1;
    Prior_m_2 = normal_rng(0.0, 1.0);
    real Prior_a_p2_1 = 0.0;
    real Prior_a_p2_2 = 1.0;
    for (I in 1:(Mode == 1 ? K_ch - 1 : 0)) {
      Prior_p_2[I] = normal_rng(Prior_a_p2_1, Prior_a_p2_2);
    }
    vector[Mode == 1 ? K_ch : 0] Prior_f_2;
    for (I in 1:(Mode == 1 ? 1 : 0)) {
      Prior_f_2 = append_row(0.0, Prior_p_2);
    }
    Prior_m_3 = normal_rng(0.0, 1.0);
    Prior_d_3 = Prior_m_3;
    Prior_m_5 = exponential_rng(1.0);
    Prior_d_5 = Prior_m_5;
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(eta[Ppc_o1_rows], sigma);
    vector[N] E_eta_1;
    E_eta_1[Prior_o1_rows] = ((Prior_m_1 + (Prior_m_3 * x[Prior_o1_rows])) + (Prior_f_2[channel[Prior_o1_rows]]));
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(E_eta_1[Prior_o1_rows], Prior_m_5);
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
