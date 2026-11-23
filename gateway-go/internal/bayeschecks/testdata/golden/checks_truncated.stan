data {
  int<lower=0> N;
  int<lower=1> J_grp;
  array[N] int<lower=1, upper=J_grp> g;
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
parameters {
  real<lower=0.1, upper=0.9> a_b;
  real<lower=0.0> a_c;
  real<lower=0.0, upper=4.0> a_e;
  real<lower=0.0, upper=5.0> a_ln;
  real<lower=-1.0, upper=3.0> a_n;
  real<lower=-2.0, upper=2.0> a_t;
  real<lower=0.0, upper=5.0> a_u;
  real<lower=0.0> s_t;
  vector<lower=0.0>[J_grp] u_g;
  vector<lower=0.0>[J_grp] v_g;
}
model {
  a_b ~ beta(2.0, 3.0);
  a_c ~ cauchy(0.0, 1.0);
  a_e ~ exponential(1.0);
  a_ln ~ lognormal(0.0, 1.0);
  a_n ~ normal(0.0, 2.0);
  a_t ~ student_t(3.0, 0.0, 1.0);
  a_u ~ uniform(0.0, 10.0);
  s_t ~ normal(0.0, a_e) T[0.0, ];
  for (I in 1:J_grp) {
    u_g[I] ~ normal(a_n, s_t) T[0.0, ];
  }
  {
    vector[J_grp] Arg2_vg = u_g;
    for (I in 1:J_grp) {
      v_g[I] ~ student_t(3.0, Arg2_vg[I], 1.0) T[0.0, ];
    }
  }
  y ~ normal(((((((a_c + (a_ln * x)) + (u_g[g])) + (v_g[g])) + a_t) + a_b) + a_u), s_t);
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
  real Prior_m_5;
  real Prior_d_5;
  real Prior_m_6;
  real Prior_d_6;
  real Prior_m_7;
  real Prior_d_7;
  real Prior_m_9;
  real Prior_d_9;
  real Prior_m_10;
  vector[Mode == 1 ? J_grp : 0] Prior_p_10;
  real Prior_m_11;
  vector[Mode == 1 ? J_grp : 0] Prior_p_11;
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
    real Prior_u_m1 = (inc_beta(2.0, 3.0, 0.1) + ((inc_beta(2.0, 3.0, 0.9) + ((-1.0) * inc_beta(2.0, 3.0, 0.1))) * uniform_rng(0.0, 1.0)));
    Prior_m_1 = inv_inc_beta(2.0, 3.0, fmin(fmax(Prior_u_m1, 0.0), 1.0));
    Prior_d_1 = Prior_m_1;
    real Prior_u_m2 = ((0.5 + (atan(((0.0 + ((-1.0) * 0.0)) * inv(1.0))) * 0.3183098861837907)) + ((1.0 + ((-1.0) * (0.5 + (atan(((0.0 + ((-1.0) * 0.0)) * inv(1.0))) * 0.3183098861837907)))) * uniform_rng(0.0, 1.0)));
    Prior_m_2 = (0.0 + (1.0 * tan((3.141592653589793 * (Prior_u_m2 + ((-1.0) * 0.5))))));
    Prior_d_2 = Prior_m_2;
    real Prior_u_m3 = (0.0 + ((((-1.0) * expm1(((-4.0) * 1.0))) + ((-1.0) * 0.0)) * uniform_rng(0.0, 1.0)));
    Prior_m_3 = (((-1.0) * log1m(fmin(fmax(Prior_u_m3, 0.0), 1.0))) * inv(1.0));
    Prior_d_3 = Prior_m_3;
    real Prior_u_m4 = (0.0 + ((Phi(((1.6094379124341003 + ((-1.0) * 0.0)) * inv(1.0))) + ((-1.0) * 0.0)) * uniform_rng(0.0, 1.0)));
    Prior_m_4 = exp((0.0 + (1.0 * inv_Phi(fmin(fmax(Prior_u_m4, 0.0), 1.0)))));
    Prior_d_4 = Prior_m_4;
    real Prior_u_m5 = (Phi((((-1.0) + ((-1.0) * 0.0)) * inv(2.0))) + ((Phi(((3.0 + ((-1.0) * 0.0)) * inv(2.0))) + ((-1.0) * Phi((((-1.0) + ((-1.0) * 0.0)) * inv(2.0))))) * uniform_rng(0.0, 1.0)));
    Prior_m_5 = (0.0 + (2.0 * inv_Phi(fmin(fmax(Prior_u_m5, 0.0), 1.0))));
    Prior_d_5 = Prior_m_5;
    real Prior_u_m6 = ((0.5 + ((step((((-2.0) + ((-1.0) * 0.0)) * inv(1.0))) + ((-1.0) * 0.5)) * inc_beta(0.5, (0.5 * 3.0), (square((((-2.0) + ((-1.0) * 0.0)) * inv(1.0))) * inv((3.0 + square((((-2.0) + ((-1.0) * 0.0)) * inv(1.0))))))))) + (((0.5 + ((step(((2.0 + ((-1.0) * 0.0)) * inv(1.0))) + ((-1.0) * 0.5)) * inc_beta(0.5, (0.5 * 3.0), (square(((2.0 + ((-1.0) * 0.0)) * inv(1.0))) * inv((3.0 + square(((2.0 + ((-1.0) * 0.0)) * inv(1.0))))))))) + ((-1.0) * (0.5 + ((step((((-2.0) + ((-1.0) * 0.0)) * inv(1.0))) + ((-1.0) * 0.5)) * inc_beta(0.5, (0.5 * 3.0), (square((((-2.0) + ((-1.0) * 0.0)) * inv(1.0))) * inv((3.0 + square((((-2.0) + ((-1.0) * 0.0)) * inv(1.0))))))))))) * uniform_rng(0.0, 1.0)));
    real Prior_s_m6 = ((2.0 * step((Prior_u_m6 + ((-1.0) * 0.5)))) + ((-1.0) * 1.0));
    real Prior_q_m6 = inv_inc_beta(0.5, (0.5 * 3.0), fmin(fmax((Prior_s_m6 * ((2.0 * Prior_u_m6) + ((-1.0) * 1.0))), 0.0), 1.0));
    Prior_m_6 = (0.0 + (1.0 * (Prior_s_m6 * sqrt(((3.0 * Prior_q_m6) * inv((1.0 + ((-1.0) * Prior_q_m6))))))));
    Prior_d_6 = Prior_m_6;
    Prior_m_7 = uniform_rng(0.0, 5.0);
    Prior_d_7 = Prior_m_7;
    real Prior_g_m9_2 = Prior_m_3;
    real Prior_u_m9 = (Phi(fmin(fmax(((0.0 + ((-1.0) * 0.0)) * inv(Prior_g_m9_2)), (-40.0)), 40.0)) + ((1.0 + ((-1.0) * Phi(fmin(fmax(((0.0 + ((-1.0) * 0.0)) * inv(Prior_g_m9_2)), (-40.0)), 40.0)))) * uniform_rng(0.0, 1.0)));
    Prior_m_9 = (0.0 + (Prior_g_m9_2 * inv_Phi(fmin(fmax(Prior_u_m9, 0.0), 1.0))));
    Prior_d_9 = Prior_m_9;
    real Prior_g_m10_1 = Prior_m_5;
    real Prior_g_m10_2 = Prior_m_9;
    real Prior_u_m10 = (Phi(fmin(fmax(((0.0 + ((-1.0) * Prior_g_m10_1)) * inv(Prior_g_m10_2)), (-40.0)), 40.0)) + ((1.0 + ((-1.0) * Phi(fmin(fmax(((0.0 + ((-1.0) * Prior_g_m10_1)) * inv(Prior_g_m10_2)), (-40.0)), 40.0)))) * uniform_rng(0.0, 1.0)));
    Prior_m_10 = (Prior_g_m10_1 + (Prior_g_m10_2 * inv_Phi(fmin(fmax(Prior_u_m10, 0.0), 1.0))));
    real Prior_a_p10_1 = Prior_m_5;
    real Prior_a_p10_2 = Prior_m_9;
    for (I in 1:(Mode == 1 ? J_grp : 0)) {
      real Prior_u_p10 = (Phi(fmin(fmax(((0.0 + ((-1.0) * Prior_a_p10_1)) * inv(Prior_a_p10_2)), (-40.0)), 40.0)) + ((1.0 + ((-1.0) * Phi(fmin(fmax(((0.0 + ((-1.0) * Prior_a_p10_1)) * inv(Prior_a_p10_2)), (-40.0)), 40.0)))) * uniform_rng(0.0, 1.0)));
      Prior_p_10[I] = (Prior_a_p10_1 + (Prior_a_p10_2 * inv_Phi(fmin(fmax(Prior_u_p10, 0.0), 1.0))));
    }
    vector[J_grp] Prior_r_10;
    for (I in 1:J_grp) {
      Prior_r_10[I] = Prior_m_10;
    }
    vector[J_grp] Prior_v_11_2 = Prior_r_10;
    real Prior_g_m11_2 = Prior_v_11_2[1];
    real Prior_u_m11 = ((0.5 + ((step(((0.0 + ((-1.0) * Prior_g_m11_2)) * inv(1.0))) + ((-1.0) * 0.5)) * inc_beta(0.5, (0.5 * 3.0), fmin(fmax((square(((0.0 + ((-1.0) * Prior_g_m11_2)) * inv(1.0))) * inv((3.0 + square(((0.0 + ((-1.0) * Prior_g_m11_2)) * inv(1.0)))))), 0.0), 1.0)))) + ((1.0 + ((-1.0) * (0.5 + ((step(((0.0 + ((-1.0) * Prior_g_m11_2)) * inv(1.0))) + ((-1.0) * 0.5)) * inc_beta(0.5, (0.5 * 3.0), fmin(fmax((square(((0.0 + ((-1.0) * Prior_g_m11_2)) * inv(1.0))) * inv((3.0 + square(((0.0 + ((-1.0) * Prior_g_m11_2)) * inv(1.0)))))), 0.0), 1.0)))))) * uniform_rng(0.0, 1.0)));
    real Prior_s_m11 = ((2.0 * step((Prior_u_m11 + ((-1.0) * 0.5)))) + ((-1.0) * 1.0));
    real Prior_q_m11 = inv_inc_beta(0.5, (0.5 * 3.0), fmin(fmax((Prior_s_m11 * ((2.0 * Prior_u_m11) + ((-1.0) * 1.0))), 0.0), 1.0));
    Prior_m_11 = (Prior_g_m11_2 + (1.0 * (Prior_s_m11 * sqrt(((3.0 * Prior_q_m11) * inv((1.0 + ((-1.0) * Prior_q_m11))))))));
    real Prior_a_p11_1 = 3.0;
    vector[Mode == 1 ? J_grp : 0] Prior_a_p11_2;
    real Prior_a_p11_3 = 1.0;
    for (I in 1:(Mode == 1 ? 1 : 0)) {
      Prior_a_p11_2 = Prior_p_10;
    }
    for (I in 1:(Mode == 1 ? J_grp : 0)) {
      real Prior_u_p11 = ((0.5 + ((step(((0.0 + ((-1.0) * Prior_a_p11_2[I])) * inv(Prior_a_p11_3))) + ((-1.0) * 0.5)) * inc_beta(0.5, (0.5 * Prior_a_p11_1), fmin(fmax((square(((0.0 + ((-1.0) * Prior_a_p11_2[I])) * inv(Prior_a_p11_3))) * inv((Prior_a_p11_1 + square(((0.0 + ((-1.0) * Prior_a_p11_2[I])) * inv(Prior_a_p11_3)))))), 0.0), 1.0)))) + ((1.0 + ((-1.0) * (0.5 + ((step(((0.0 + ((-1.0) * Prior_a_p11_2[I])) * inv(Prior_a_p11_3))) + ((-1.0) * 0.5)) * inc_beta(0.5, (0.5 * Prior_a_p11_1), fmin(fmax((square(((0.0 + ((-1.0) * Prior_a_p11_2[I])) * inv(Prior_a_p11_3))) * inv((Prior_a_p11_1 + square(((0.0 + ((-1.0) * Prior_a_p11_2[I])) * inv(Prior_a_p11_3)))))), 0.0), 1.0)))))) * uniform_rng(0.0, 1.0)));
      real Prior_s_p11 = ((2.0 * step((Prior_u_p11 + ((-1.0) * 0.5)))) + ((-1.0) * 1.0));
      real Prior_q_p11 = inv_inc_beta(0.5, (0.5 * Prior_a_p11_1), fmin(fmax((Prior_s_p11 * ((2.0 * Prior_u_p11) + ((-1.0) * 1.0))), 0.0), 1.0));
      Prior_p_11[I] = (Prior_a_p11_2[I] + (Prior_a_p11_3 * (Prior_s_p11 * sqrt(((Prior_a_p11_1 * Prior_q_p11) * inv((1.0 + ((-1.0) * Prior_q_p11))))))));
    }
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(((((((a_c + (a_ln * x[Ppc_o1_rows])) + (u_g[g[Ppc_o1_rows]])) + (v_g[g[Ppc_o1_rows]])) + a_t) + a_b) + a_u), s_t);
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(((((((Prior_m_2 + (Prior_m_4 * x[Prior_o1_rows])) + (Prior_p_10[g[Prior_o1_rows]])) + (Prior_p_11[g[Prior_o1_rows]])) + Prior_m_6) + Prior_m_1) + Prior_m_7), Prior_m_9);
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
