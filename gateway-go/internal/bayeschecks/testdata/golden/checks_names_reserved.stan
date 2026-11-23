data {
  int<lower=0> N;
  vector[N] V_x;
  vector[N] V_y;
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
generated quantities {
  real Prior_m_1;
  real Prior_d_1;
  real Prior_m_2;
  real Prior_d_2;
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
    vector[N] eta = (V_a + (V_b * V_x));
    Prior_m_1 = normal_rng(0.0, 2.5);
    Prior_d_1 = Prior_m_1;
    Prior_m_2 = normal_rng(0.0, 2.5);
    Prior_d_2 = Prior_m_2;
    Prior_m_3 = exponential_rng(1.0);
    Prior_d_3 = Prior_m_3;
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(eta[Ppc_o1_rows], sigma);
    vector[N] E_eta_1;
    E_eta_1[Prior_o1_rows] = (Prior_m_1 + (Prior_m_2 * V_x[Prior_o1_rows]));
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(E_eta_1[Prior_o1_rows], Prior_m_3);
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
