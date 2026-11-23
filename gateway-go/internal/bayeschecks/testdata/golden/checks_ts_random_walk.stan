data {
  int<lower=0> N;
  vector[N] y;
  int<lower=0> N_lag;
  array[N_lag] int<lower=1, upper=N> Rows_lag;
  array[N_lag] int<lower=1, upper=N> Lag1;
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
  real delta;
  real<lower=0.0> sigma;
}
model {
  delta ~ normal(0.0, 300.0);
  sigma ~ exponential((1.0 / 300.0));
  y[Rows_lag] ~ normal(((y[Lag1]) + delta), sigma);
}
generated quantities {
  real Prior_m_1;
  real Prior_d_1;
  real Prior_m_2;
  real Prior_d_2;
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
    Prior_m_1 = normal_rng(0.0, 300.0);
    Prior_d_1 = Prior_m_1;
    real Prior_g_m2_1 = (1.0 / 300.0);
    Prior_m_2 = (exponential_rng(fmin(fmax(Prior_g_m2_1, 1e-300), 1e+300)) + (0.0 * Prior_g_m2_1));
    Prior_d_2 = Prior_m_2;
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(((y[Lag1[Ppc_o1_rows]]) + delta), sigma);
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(((y[Lag1[Prior_o1_rows]]) + Prior_m_1), Prior_m_2);
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
