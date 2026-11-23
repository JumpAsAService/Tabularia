data {
  int<lower=0> N;
  vector[N] V_d1;
  vector[N] V_d10;
  vector[N] V_d11;
  vector[N] V_d12;
  vector[N] V_d13;
  vector[N] V_d14;
  vector[N] V_d15;
  vector[N] V_d16;
  vector[N] V_d17;
  vector[N] V_d18;
  vector[N] V_d19;
  vector[N] V_d2;
  vector[N] V_d20;
  vector[N] V_d21;
  vector[N] V_d22;
  vector[N] V_d23;
  vector[N] V_d3;
  vector[N] V_d4;
  vector[N] V_d5;
  vector[N] V_d6;
  vector[N] V_d7;
  vector[N] V_d8;
  vector[N] V_d9;
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
  real beta_sum;
  real<lower=0.0> sigma;
}
model {
  alpha ~ normal(0.0, 2.5);
  beta_sum ~ normal(0.0, 1.0);
  sigma ~ exponential(1.0);
  y ~ normal((alpha + (beta_sum * ((((((((((((((((((((((V_d1 + V_d2) + V_d3) + V_d4) + V_d5) + V_d6) + V_d7) + V_d8) + V_d9) + V_d10) + V_d11) + V_d12) + V_d13) + V_d14) + V_d15) + V_d16) + V_d17) + V_d18) + V_d19) + V_d20) + V_d21) + V_d22) + V_d23))), sigma);
}
generated quantities {
  real Prior_m_1;
  real Prior_d_1;
  real Prior_m_2;
  real Prior_d_2;
  real Prior_m_26;
  real Prior_d_26;
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
    Prior_m_2 = normal_rng(0.0, 1.0);
    Prior_d_2 = Prior_m_2;
    Prior_m_26 = exponential_rng(1.0);
    Prior_d_26 = Prior_m_26;
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng((alpha + (beta_sum * ((((((((((((((((((((((V_d1[Ppc_o1_rows] + V_d2[Ppc_o1_rows]) + V_d3[Ppc_o1_rows]) + V_d4[Ppc_o1_rows]) + V_d5[Ppc_o1_rows]) + V_d6[Ppc_o1_rows]) + V_d7[Ppc_o1_rows]) + V_d8[Ppc_o1_rows]) + V_d9[Ppc_o1_rows]) + V_d10[Ppc_o1_rows]) + V_d11[Ppc_o1_rows]) + V_d12[Ppc_o1_rows]) + V_d13[Ppc_o1_rows]) + V_d14[Ppc_o1_rows]) + V_d15[Ppc_o1_rows]) + V_d16[Ppc_o1_rows]) + V_d17[Ppc_o1_rows]) + V_d18[Ppc_o1_rows]) + V_d19[Ppc_o1_rows]) + V_d20[Ppc_o1_rows]) + V_d21[Ppc_o1_rows]) + V_d22[Ppc_o1_rows]) + V_d23[Ppc_o1_rows]))), sigma);
    array[Prior_o1_n] real Prior_o1_draw = normal_rng((Prior_m_1 + (Prior_m_2 * ((((((((((((((((((((((V_d1[Prior_o1_rows] + V_d2[Prior_o1_rows]) + V_d3[Prior_o1_rows]) + V_d4[Prior_o1_rows]) + V_d5[Prior_o1_rows]) + V_d6[Prior_o1_rows]) + V_d7[Prior_o1_rows]) + V_d8[Prior_o1_rows]) + V_d9[Prior_o1_rows]) + V_d10[Prior_o1_rows]) + V_d11[Prior_o1_rows]) + V_d12[Prior_o1_rows]) + V_d13[Prior_o1_rows]) + V_d14[Prior_o1_rows]) + V_d15[Prior_o1_rows]) + V_d16[Prior_o1_rows]) + V_d17[Prior_o1_rows]) + V_d18[Prior_o1_rows]) + V_d19[Prior_o1_rows]) + V_d20[Prior_o1_rows]) + V_d21[Prior_o1_rows]) + V_d22[Prior_o1_rows]) + V_d23[Prior_o1_rows]))), Prior_m_26);
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
