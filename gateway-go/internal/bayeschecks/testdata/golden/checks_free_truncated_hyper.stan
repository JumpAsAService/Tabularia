data {
  int<lower=0> N;
  int<lower=1> J_dst;
  int<lower=1> J_sch;
  array[N] int<lower=1, upper=J_dst> d;
  array[N] int<lower=1, upper=J_sch> s;
  vector[N] y;
  array[J_sch] int<lower=1, upper=J_dst> Up_d_sch;
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
parameters {
  real<lower=0.0> s_hyper;
  real<lower=0.0> tau;
  vector<offset=0.0, multiplier=tau>[J_dst] mu_district;
  vector<lower=0.0>[J_sch] sigma_school;
}
model {
  s_hyper ~ exponential(1.0);
  tau ~ normal(0.0, s_hyper) T[0.0, ];
  mu_district ~ normal(0.0, tau);
  {
    vector[J_sch] Arg1_sigs = (mu_district[Up_d_sch]);
    for (I in 1:J_sch) {
      sigma_school[I] ~ normal(Arg1_sigs[I], s_hyper) T[0.0, ];
    }
  }
  y ~ normal((mu_district[d]), (sigma_school[s]));
}
generated quantities {
  real Prior_m_3;
  real Prior_d_3;
  real Prior_m_4;
  real Prior_d_4;
  real Prior_m_5;
  vector[Mode == 1 ? J_dst : 0] Prior_p_5;
  real Prior_m_6;
  vector[Mode == 1 ? J_sch : 0] Prior_p_6;
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
    Prior_m_3 = exponential_rng(1.0);
    Prior_d_3 = Prior_m_3;
    real Prior_g_m4_2 = Prior_m_3;
    real Prior_u_m4 = (Phi(fmin(fmax(((0.0 + ((-1.0) * 0.0)) * inv(Prior_g_m4_2)), (-40.0)), 40.0)) + ((1.0 + ((-1.0) * Phi(fmin(fmax(((0.0 + ((-1.0) * 0.0)) * inv(Prior_g_m4_2)), (-40.0)), 40.0)))) * uniform_rng(0.0, 1.0)));
    Prior_m_4 = (0.0 + (Prior_g_m4_2 * inv_Phi(fmin(fmax(Prior_u_m4, 0.0), 1.0))));
    Prior_d_4 = Prior_m_4;
    real Prior_g_m5_2 = Prior_m_4;
    Prior_m_5 = (normal_rng(0.0, fmin(fmax(Prior_g_m5_2, 1e-300), 1e+300)) + (0.0 * Prior_g_m5_2));
    real Prior_a_p5_1 = 0.0;
    real Prior_a_p5_2 = Prior_m_4;
    for (I in 1:(Mode == 1 ? J_dst : 0)) {
      Prior_p_5[I] = (normal_rng(Prior_a_p5_1, fmin(fmax(Prior_a_p5_2, 1e-300), 1e+300)) + (0.0 * Prior_a_p5_2));
    }
    vector[J_dst] Prior_r_5;
    for (I in 1:J_dst) {
      Prior_r_5[I] = Prior_m_5;
    }
    vector[J_sch] Prior_v_6_1 = (Prior_r_5[Up_d_sch]);
    real Prior_g_m6_1 = Prior_v_6_1[1];
    real Prior_g_m6_2 = Prior_m_3;
    real Prior_u_m6 = (Phi(fmin(fmax(((0.0 + ((-1.0) * Prior_g_m6_1)) * inv(Prior_g_m6_2)), (-40.0)), 40.0)) + ((1.0 + ((-1.0) * Phi(fmin(fmax(((0.0 + ((-1.0) * Prior_g_m6_1)) * inv(Prior_g_m6_2)), (-40.0)), 40.0)))) * uniform_rng(0.0, 1.0)));
    Prior_m_6 = (Prior_g_m6_1 + (Prior_g_m6_2 * inv_Phi(fmin(fmax(Prior_u_m6, 0.0), 1.0))));
    vector[Mode == 1 ? J_sch : 0] Prior_a_p6_1;
    real Prior_a_p6_2 = Prior_m_3;
    for (I in 1:(Mode == 1 ? 1 : 0)) {
      Prior_a_p6_1 = (Prior_p_5[Up_d_sch]);
    }
    for (I in 1:(Mode == 1 ? J_sch : 0)) {
      real Prior_u_p6 = (Phi(fmin(fmax(((0.0 + ((-1.0) * Prior_a_p6_1[I])) * inv(Prior_a_p6_2)), (-40.0)), 40.0)) + ((1.0 + ((-1.0) * Phi(fmin(fmax(((0.0 + ((-1.0) * Prior_a_p6_1[I])) * inv(Prior_a_p6_2)), (-40.0)), 40.0)))) * uniform_rng(0.0, 1.0)));
      Prior_p_6[I] = (Prior_a_p6_1[I] + (Prior_a_p6_2 * inv_Phi(fmin(fmax(Prior_u_p6, 0.0), 1.0))));
    }
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng((mu_district[d[Ppc_o1_rows]]), (sigma_school[s[Ppc_o1_rows]]));
    array[Prior_o1_n] real Prior_o1_draw = normal_rng((Prior_p_5[d[Prior_o1_rows]]), (Prior_p_6[s[Prior_o1_rows]]));
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
