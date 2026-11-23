data {
  int<lower=0> N;
  vector[N] score;
  vector[N] V_n2;
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
  real V_n3;
  real V_n4;
  real<lower=0.0> V_n5;
}
model {
  vector[N] V_n6 = (V_n3 + (V_n4 * V_n2));
  V_n3 ~ normal(0.0, 10.0);
  V_n4 ~ normal(0.0, 1.0);
  V_n5 ~ exponential(1.0);
  score ~ normal(V_n6, V_n5);
}
generated quantities {
  real Prior_m_3;
  real Prior_d_3;
  real Prior_m_4;
  real Prior_d_4;
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
    vector[N] V_n6 = (V_n3 + (V_n4 * V_n2));
    Prior_m_3 = normal_rng(0.0, 10.0);
    Prior_d_3 = Prior_m_3;
    Prior_m_4 = normal_rng(0.0, 1.0);
    Prior_d_4 = Prior_m_4;
    Prior_m_5 = exponential_rng(1.0);
    Prior_d_5 = Prior_m_5;
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(V_n6[Ppc_o1_rows], V_n5);
    vector[N] E_n6_1;
    E_n6_1[Prior_o1_rows] = (Prior_m_3 + (Prior_m_4 * V_n2[Prior_o1_rows]));
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(E_n6_1[Prior_o1_rows], Prior_m_5);
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
