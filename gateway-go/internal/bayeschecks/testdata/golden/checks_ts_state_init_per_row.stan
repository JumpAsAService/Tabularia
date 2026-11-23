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
  int<lower=0> Ppc_o1_m;
  int<lower=0> Ppc_o1_slots;
  real Ppc_o1_scale;
  real Ppc_o1_shift;
  int<lower=0> Ppc_o1_binned;
  int<lower=0> Ppc_o1_rep;
}
parameters {
  real alpha;
  real<lower=0.0> sigma_level;
  real<lower=0.0> sigma_obs;
  real<lower=0.0> sigma_start;
  real<lower=0.0> sigma_trend;
  vector[N] trend;
  vector[N] Z_level;
}
transformed parameters {
  vector[N] level;
  {
    vector[N] spread = rep_vector((sigma_start * 2.0), N);
    vector[N] start = (alpha + (0.5 * trend));
    level[Rows_first] = (start[Rows_first] + (spread[Rows_first] .* Z_level[Rows_first]));
  }
  for (I in 1:N_next) {
    level[Rows_next[I]] = (level[Prev[I]] + (sigma_level * Z_level[Rows_next[I]]));
  }
}
model {
  vector[N] spread = rep_vector((sigma_start * 2.0), N);
  vector[N] start = (alpha + (0.5 * trend));
  alpha ~ normal(0.0, 2.5);
  sigma_level ~ exponential(1.0);
  sigma_obs ~ exponential(1.0);
  sigma_start ~ exponential(1.0);
  sigma_trend ~ exponential(1.0);
  trend[Rows_first] ~ normal(0.0, 10.0);
  trend[Rows_next] ~ normal(trend[Prev], sigma_trend);
  Z_level ~ std_normal();
  y ~ normal(level, sigma_obs);
}
generated quantities {
  real Prior_m_1;
  real Prior_m_2;
  real Prior_m_3;
  real Prior_m_4;
  real Prior_m_5;
  real Prior_m_8;
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
    Prior_m_2 = exponential_rng(1.0);
    Prior_m_3 = exponential_rng(1.0);
    Prior_m_4 = exponential_rng(1.0);
    Prior_m_5 = exponential_rng(1.0);
    Prior_m_8 = normal_rng(0.0, 10.0);
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(level[Ppc_o1_rows], sigma_obs);
    vector[Ppc_o1_m] Ppc_o1_y = to_vector(Ppc_o1_post);
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
