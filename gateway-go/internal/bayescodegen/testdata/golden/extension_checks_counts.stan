data {
  int<lower=0> N;
  array[N] int<lower=0, upper=1> bought;
  array[N] int<lower=0> visits;
  array[N] int<lower=0> k;
  array[N] int<lower=0> trials;
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
  int<lower=0> Ppc_o1_units;
  array[Ppc_o1_m] int<lower=0> Ppc_o1_slot;
  int<lower=0> Ppc_o1_binned;
  int<lower=0> Ppc_o1_rep;
  int<lower=0> Ppc_o2_n;
  array[Ppc_o2_n] int<lower=0> Ppc_o2_rows;
  int<lower=0> Prior_o2_n;
  array[Prior_o2_n] int<lower=0> Prior_o2_rows;
  int<lower=0> Ppc_o2_m;
  int<lower=0> Ppc_o2_slots;
  real Ppc_o2_scale;
  real Ppc_o2_shift;
  int<lower=0> Ppc_o2_units;
  array[Ppc_o2_m] int<lower=0> Ppc_o2_slot;
  int<lower=0> Ppc_o2_binned;
  int<lower=0> Ppc_o2_rep;
  int<lower=0> Ppc_o3_n;
  array[Ppc_o3_n] int<lower=0> Ppc_o3_rows;
  int<lower=0> Prior_o3_n;
  array[Prior_o3_n] int<lower=0> Prior_o3_rows;
  int<lower=0> Ppc_o3_m;
  int<lower=0> Ppc_o3_slots;
  real Ppc_o3_scale;
  real Ppc_o3_shift;
  int<lower=0> Ppc_o3_units;
  array[Ppc_o3_m] int<lower=0> Ppc_o3_slot;
  int<lower=0> Ppc_o3_binned;
  int<lower=0> Ppc_o3_rep;
}
parameters {
  real alpha;
  real b_x;
  real<lower=0.0> phi;
  real<lower=0.0> sigma;
}
model {
  vector[N] eta = (alpha + (b_x * x));
  alpha ~ normal(0.0, 2.5);
  b_x ~ normal(0.0, 1.0);
  phi ~ exponential(1.0);
  sigma ~ normal(0.0, 2.0);
  bought ~ bernoulli_logit(alpha);
  visits ~ neg_binomial_2_log(eta, phi);
  k ~ binomial_logit(trials, eta);
  y ~ normal(eta, sigma);
}
generated quantities {
  real Prior_m_1;
  real Prior_m_3;
  real Prior_m_8;
  real Prior_m_9;
  array[Ppc_on] real Ppc_o1_mean;
  array[Ppc_on] real Ppc_o1_sd;
  array[Ppc_on] real Ppc_o1_min;
  array[Ppc_on] real Ppc_o1_max;
  array[Ppc_on] real Ppc_o1_low;
  array[Ppc_on] real Ppc_o1_median;
  array[Ppc_on] real Ppc_o1_zeros;
  array[Ppc_o1_slots] int Ppc_o1_count;
  vector[Ppc_o1_units] Ppc_o1_usum;
  array[Ppc_o1_rep] real Ppc_o1_yrep;
  array[Ppc_on] real Ppc_o2_mean;
  array[Ppc_on] real Ppc_o2_sd;
  array[Ppc_on] real Ppc_o2_min;
  array[Ppc_on] real Ppc_o2_max;
  array[Ppc_on] real Ppc_o2_low;
  array[Ppc_on] real Ppc_o2_median;
  array[Ppc_on] real Ppc_o2_zeros;
  array[Ppc_o2_slots] int Ppc_o2_count;
  vector[Ppc_o2_units] Ppc_o2_usum;
  array[Ppc_o2_rep] real Ppc_o2_yrep;
  array[Ppc_on] real Ppc_o3_mean;
  array[Ppc_on] real Ppc_o3_sd;
  array[Ppc_on] real Ppc_o3_min;
  array[Ppc_on] real Ppc_o3_max;
  array[Ppc_on] real Ppc_o3_low;
  array[Ppc_on] real Ppc_o3_median;
  array[Ppc_on] real Ppc_o3_zeros;
  array[Ppc_o3_slots] int Ppc_o3_count;
  vector[Ppc_o3_units] Ppc_o3_usum;
  array[Ppc_o3_rep] real Ppc_o3_yrep;
  {
    vector[N] eta = (alpha + (b_x * x));
    Prior_m_1 = normal_rng(0.0, 2.5);
    Prior_m_3 = normal_rng(0.0, 1.0);
    Prior_m_8 = exponential_rng(1.0);
    real Prior_u_9 = uniform_rng(Phi(((0.0 + ((-1.0) * 0.0)) * inv(2.0))), 1.0);
    Prior_m_9 = (0.0 + (2.0 * inv_Phi(Prior_u_9)));
    array[Ppc_o1_n] int Ppc_o1_post = bernoulli_logit_rng(rep_vector(alpha, Ppc_o1_n));
    array[Prior_o1_n] int Prior_o1_draw = bernoulli_logit_rng(rep_vector(Prior_m_1, Prior_o1_n));
    vector[Ppc_o1_m] Ppc_o1_y = to_vector(append_array(Ppc_o1_post, Prior_o1_draw));
    Ppc_o1_count = rep_array(0, Ppc_o1_slots);
    Ppc_o1_usum = rep_vector(0.0, Ppc_o1_units);
    for (I in 1:Ppc_on) {
      Ppc_o1_mean[I] = mean(Ppc_o1_y);
      Ppc_o1_sd[I] = sd(Ppc_o1_y);
      Ppc_o1_min[I] = min(Ppc_o1_y);
      Ppc_o1_max[I] = max(Ppc_o1_y);
      Ppc_o1_low[I] = quantile(Ppc_o1_y, 0.1);
      Ppc_o1_median[I] = quantile(Ppc_o1_y, 0.5);
      Ppc_o1_zeros[I] = 0.0;
    }
    for (I in 1:Ppc_o1_binned) {
      int Ppc_o1_b = to_int(fmin(fmax(floor(((Ppc_o1_y[I] * Ppc_o1_scale) + Ppc_o1_shift)), 1.0), Ppc_o1_slots));
      Ppc_o1_count[Ppc_o1_b] = (Ppc_o1_count[Ppc_o1_b] + 1);
      Ppc_o1_usum[Ppc_o1_slot[I]] = (Ppc_o1_usum[Ppc_o1_slot[I]] + Ppc_o1_y[I]);
      Ppc_o1_zeros[1] = (Ppc_o1_zeros[1] + (1.0 + ((-1.0) * fmin(Ppc_o1_y[I], 1.0))));
    }
    for (I in 1:Ppc_o1_rep) {
      Ppc_o1_yrep[I] = Ppc_o1_y[I];
    }
    array[Ppc_o2_n] int Ppc_o2_post = neg_binomial_2_log_rng(eta[Ppc_o2_rows], phi);
    vector[N] E_eta_1;
    E_eta_1[Prior_o2_rows] = (Prior_m_1 + (Prior_m_3 * x[Prior_o2_rows]));
    array[Prior_o2_n] int Prior_o2_draw = neg_binomial_2_log_rng(E_eta_1[Prior_o2_rows], Prior_m_8);
    vector[Ppc_o2_m] Ppc_o2_y = to_vector(append_array(Ppc_o2_post, Prior_o2_draw));
    Ppc_o2_count = rep_array(0, Ppc_o2_slots);
    Ppc_o2_usum = rep_vector(0.0, Ppc_o2_units);
    for (I in 1:Ppc_on) {
      Ppc_o2_mean[I] = mean(Ppc_o2_y);
      Ppc_o2_sd[I] = sd(Ppc_o2_y);
      Ppc_o2_min[I] = min(Ppc_o2_y);
      Ppc_o2_max[I] = max(Ppc_o2_y);
      Ppc_o2_low[I] = quantile(Ppc_o2_y, 0.1);
      Ppc_o2_median[I] = quantile(Ppc_o2_y, 0.5);
      Ppc_o2_zeros[I] = 0.0;
    }
    for (I in 1:Ppc_o2_binned) {
      int Ppc_o2_b = to_int(fmin(fmax(floor(((Ppc_o2_y[I] * Ppc_o2_scale) + Ppc_o2_shift)), 1.0), Ppc_o2_slots));
      Ppc_o2_count[Ppc_o2_b] = (Ppc_o2_count[Ppc_o2_b] + 1);
      Ppc_o2_usum[Ppc_o2_slot[I]] = (Ppc_o2_usum[Ppc_o2_slot[I]] + Ppc_o2_y[I]);
      Ppc_o2_zeros[1] = (Ppc_o2_zeros[1] + (1.0 + ((-1.0) * fmin(Ppc_o2_y[I], 1.0))));
    }
    for (I in 1:Ppc_o2_rep) {
      Ppc_o2_yrep[I] = Ppc_o2_y[I];
    }
    array[Ppc_o3_n] int Ppc_o3_post = binomial_rng(trials[Ppc_o3_rows], inv_logit(eta[Ppc_o3_rows]));
    vector[N] E_eta_2;
    E_eta_2[Prior_o3_rows] = (Prior_m_1 + (Prior_m_3 * x[Prior_o3_rows]));
    array[Prior_o3_n] int Prior_o3_draw = binomial_rng(trials[Prior_o3_rows], inv_logit(E_eta_2[Prior_o3_rows]));
    vector[Ppc_o3_m] Ppc_o3_y = to_vector(append_array(Ppc_o3_post, Prior_o3_draw));
    Ppc_o3_count = rep_array(0, Ppc_o3_slots);
    Ppc_o3_usum = rep_vector(0.0, Ppc_o3_units);
    for (I in 1:Ppc_on) {
      Ppc_o3_mean[I] = mean(Ppc_o3_y);
      Ppc_o3_sd[I] = sd(Ppc_o3_y);
      Ppc_o3_min[I] = min(Ppc_o3_y);
      Ppc_o3_max[I] = max(Ppc_o3_y);
      Ppc_o3_low[I] = quantile(Ppc_o3_y, 0.1);
      Ppc_o3_median[I] = quantile(Ppc_o3_y, 0.5);
      Ppc_o3_zeros[I] = 0.0;
    }
    for (I in 1:Ppc_o3_binned) {
      int Ppc_o3_b = to_int(fmin(fmax(floor(((Ppc_o3_y[I] * Ppc_o3_scale) + Ppc_o3_shift)), 1.0), Ppc_o3_slots));
      Ppc_o3_count[Ppc_o3_b] = (Ppc_o3_count[Ppc_o3_b] + 1);
      Ppc_o3_usum[Ppc_o3_slot[I]] = (Ppc_o3_usum[Ppc_o3_slot[I]] + Ppc_o3_y[I]);
      Ppc_o3_zeros[1] = (Ppc_o3_zeros[1] + (1.0 + ((-1.0) * fmin(Ppc_o3_y[I], 1.0))));
    }
    for (I in 1:Ppc_o3_rep) {
      Ppc_o3_yrep[I] = Ppc_o3_y[I];
    }
  }
}
