data {
  int<lower=0> N;
  vector[N] y;
  int<lower=0> N_lag;
  array[N_lag] int<lower=1, upper=N> Rows_lag;
  array[N_lag] int<lower=1, upper=N> Lag1;
  array[N_lag] int<lower=1, upper=N> Lag2;
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
  real mu;
  real phi1;
  real phi2;
  real<lower=0.0> sigma;
}
model {
  mu ~ normal(0.0, 2.5);
  phi1 ~ normal(0.0, 0.5);
  phi2 ~ normal(0.0, 0.5);
  sigma ~ exponential(1.0);
  y[Rows_lag] ~ normal(((mu + (phi1 * ((y[Lag1]) - mu))) + (phi2 * ((y[Lag2]) - mu))), sigma);
}
generated quantities {
  real Prior_m_1;
  real Prior_d_1;
  real Prior_m_2;
  real Prior_d_2;
  real Prior_m_3;
  real Prior_d_3;
  real Prior_m_4;
  real Prior_d_4;
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
    Prior_m_1 = normal_rng(0.0, 2.5);
    Prior_d_1 = Prior_m_1;
    Prior_m_2 = normal_rng(0.0, 0.5);
    Prior_d_2 = Prior_m_2;
    Prior_m_3 = normal_rng(0.0, 0.5);
    Prior_d_3 = Prior_m_3;
    Prior_m_4 = exponential_rng(1.0);
    Prior_d_4 = Prior_m_4;
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(((mu + (phi1 * ((y[Lag1[Ppc_o1_rows]]) - mu))) + (phi2 * ((y[Lag2[Ppc_o1_rows]]) - mu))), sigma);
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(((Prior_m_1 + (Prior_m_2 * ((y[Lag1[Prior_o1_rows]]) - Prior_m_1))) + (Prior_m_3 * ((y[Lag2[Prior_o1_rows]]) - Prior_m_1))), Prior_m_4);
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
