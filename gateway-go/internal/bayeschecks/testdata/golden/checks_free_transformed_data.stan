data {
  int<lower=0> N;
  int<lower=1> J_grp;
  array[N] int<lower=1, upper=J_grp> g;
  vector[J_grp] h;
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
  int<lower=0> Ppc_o1_units;
  array[Ppc_o1_binned] int<lower=0> Ppc_o1_slot;
  int<lower=0> Ppc_o1_rep;
}
transformed data {
  real half = (log(2.0) / 2.0);
  vector[J_grp] log_area = log(h);
  vector[N] log_price = log(x);
  vector[N] log_price_sq = square(log_price);
}
parameters {
  real a;
  real b_area;
  real b_price;
  real b_curve;
  real<lower=0.0> s;
}
model {
  a ~ normal(0.0, 2.5);
  b_area ~ normal(0.0, 1.0);
  b_price ~ normal(0.0, 1.0);
  b_curve ~ normal(0.0, 1.0);
  s ~ exponential(1.0);
  y ~ normal((((a + (b_price * log_price)) + (b_curve * log_price_sq)) + ((b_area * half) * (log_area[g]))), s);
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
  real Prior_m_9;
  real Prior_d_9;
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
    Prior_m_1 = normal_rng(0.0, 2.5);
    Prior_d_1 = Prior_m_1;
    Prior_m_2 = normal_rng(0.0, 1.0);
    Prior_d_2 = Prior_m_2;
    Prior_m_3 = normal_rng(0.0, 1.0);
    Prior_d_3 = Prior_m_3;
    Prior_m_4 = normal_rng(0.0, 1.0);
    Prior_d_4 = Prior_m_4;
    Prior_m_9 = exponential_rng(1.0);
    Prior_d_9 = Prior_m_9;
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng((((a + (b_price * log_price[Ppc_o1_rows])) + (b_curve * log_price_sq[Ppc_o1_rows])) + ((b_area * half) * (log_area[g[Ppc_o1_rows]]))), s);
    array[Prior_o1_n] real Prior_o1_draw = normal_rng((((Prior_m_1 + (Prior_m_3 * log_price[Prior_o1_rows])) + (Prior_m_4 * log_price_sq[Prior_o1_rows])) + ((Prior_m_2 * half) * (log_area[g[Prior_o1_rows]]))), Prior_m_9);
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
