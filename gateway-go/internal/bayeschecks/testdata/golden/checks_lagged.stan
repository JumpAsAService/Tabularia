data {
  int<lower=0> N;
  vector[N] x;
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
  real a;
  real b_x;
  real phi;
  real<lower=0.0> s;
}
model {
  vector[N] eta = (a + (b_x * x));
  vector[N] m;
  m[Rows_lag] = (eta[Rows_lag] + (phi * (y[Lag1])));
  a ~ normal(0.0, 2.5);
  b_x ~ normal(0.0, 1.0);
  phi ~ normal(0.0, 0.5);
  s ~ exponential(1.0);
  y[Rows_lag] ~ normal(m[Rows_lag], s);
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
    vector[N] eta = (a + (b_x * x));
    vector[N] m;
    m[Rows_lag] = (eta[Rows_lag] + (phi * (y[Lag1])));
    Prior_m_1 = normal_rng(0.0, 2.5);
    Prior_d_1 = Prior_m_1;
    Prior_m_2 = normal_rng(0.0, 1.0);
    Prior_d_2 = Prior_m_2;
    Prior_m_3 = normal_rng(0.0, 0.5);
    Prior_d_3 = Prior_m_3;
    Prior_m_4 = exponential_rng(1.0);
    Prior_d_4 = Prior_m_4;
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(m[Rows_lag[Ppc_o1_rows]], s);
    vector[N] E_eta_1;
    E_eta_1[Rows_lag[Prior_o1_rows]] = (Prior_m_1 + (Prior_m_2 * x[Rows_lag[Prior_o1_rows]]));
    vector[N] E_m_1;
    E_m_1[Rows_lag[Prior_o1_rows]] = (E_eta_1[Rows_lag[Prior_o1_rows]] + (Prior_m_3 * (y[Lag1[Prior_o1_rows]])));
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(E_m_1[Rows_lag[Prior_o1_rows]], Prior_m_4);
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
