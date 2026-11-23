data {
  int<lower=0> N;
  int<lower=1> J_grp;
  array[N] int<lower=1, upper=J_grp> g;
  vector[N] w;
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
  real mu_a;
  real mu_b;
  real mu_c;
  real<lower=0.0> s;
  real<lower=0.0> tau_a;
  real<lower=0.0> tau_b;
  real<lower=0.0> tau_c;
  matrix[3, J_grp] Z_eff;
  cholesky_factor_corr[3] L_eff;
}
transformed parameters {
  vector[J_grp] a_g;
  vector[J_grp] b_g;
  vector[J_grp] c_g;
  {
    matrix[3, J_grp] W_eff = diag_pre_multiply(to_vector([tau_a, tau_b, tau_c]), L_eff) * Z_eff;
    a_g = mu_a + to_vector(W_eff[1]);
    b_g = mu_b + to_vector(W_eff[2]);
    c_g = mu_c + to_vector(W_eff[3]);
  }
}
model {
  mu_a ~ normal(0.0, 1.0);
  mu_b ~ normal(0.0, 1.0);
  mu_c ~ normal(0.0, 1.0);
  s ~ exponential(1.0);
  tau_a ~ exponential(1.0);
  tau_b ~ exponential(1.0);
  tau_c ~ exponential(1.0);
  to_vector(Z_eff) ~ std_normal();
  L_eff ~ lkj_corr_cholesky(1.5);
  y ~ normal((((a_g[g]) + ((b_g[g]) .* x)) + ((c_g[g]) .* w)), s);
}
generated quantities {
  matrix[3, 3] rho_g = multiply_lower_tri_self_transpose(L_eff);
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
  real Prior_m_8;
  real Prior_d_8;
  real Prior_m_9_1;
  real Prior_m_9_2;
  real Prior_m_9_3;
  real Prior_m_9_rho;
  vector[Mode == 1 ? J_grp : 0] Prior_p_9_1;
  vector[Mode == 1 ? J_grp : 0] Prior_p_9_2;
  vector[Mode == 1 ? J_grp : 0] Prior_p_9_3;
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
    Prior_m_2 = normal_rng(0.0, 1.0);
    Prior_d_2 = Prior_m_2;
    Prior_m_3 = normal_rng(0.0, 1.0);
    Prior_d_3 = Prior_m_3;
    Prior_m_4 = normal_rng(0.0, 1.0);
    Prior_d_4 = Prior_m_4;
    Prior_m_5 = exponential_rng(1.0);
    Prior_d_5 = Prior_m_5;
    Prior_m_6 = exponential_rng(1.0);
    Prior_d_6 = Prior_m_6;
    Prior_m_7 = exponential_rng(1.0);
    Prior_d_7 = Prior_m_7;
    Prior_m_8 = exponential_rng(1.0);
    Prior_d_8 = Prior_m_8;
    real Prior_g_m9_1_1 = Prior_m_2;
    real Prior_g_m9_1_2 = Prior_m_6;
    Prior_m_9_1 = (normal_rng(fmin(fmax(Prior_g_m9_1_1, (-1e+300)), 1e+300), fmin(fmax(Prior_g_m9_1_2, 1e-300), 1e+300)) + (0.0 * (Prior_g_m9_1_1 + Prior_g_m9_1_2)));
    real Prior_g_m9_2_1 = Prior_m_3;
    real Prior_g_m9_2_2 = Prior_m_7;
    Prior_m_9_2 = (normal_rng(fmin(fmax(Prior_g_m9_2_1, (-1e+300)), 1e+300), fmin(fmax(Prior_g_m9_2_2, 1e-300), 1e+300)) + (0.0 * (Prior_g_m9_2_1 + Prior_g_m9_2_2)));
    real Prior_g_m9_3_1 = Prior_m_4;
    real Prior_g_m9_3_2 = Prior_m_8;
    Prior_m_9_3 = (normal_rng(fmin(fmax(Prior_g_m9_3_1, (-1e+300)), 1e+300), fmin(fmax(Prior_g_m9_3_2, 1e-300), 1e+300)) + (0.0 * (Prior_g_m9_3_1 + Prior_g_m9_3_2)));
    Prior_m_9_rho = ((2.0 * beta_rng((1.5 + 0.5), (1.5 + 0.5))) + ((-1.0) * 1.0));
    real Prior_l_9_2_1;
    real Prior_l_9_2_2;
    real Prior_l_9_3_1;
    real Prior_l_9_3_2;
    real Prior_l_9_3_3;
    for (I in 1:(Mode == 1 ? 1 : 0)) {
      real Prior_oe_9 = (1.5 + 0.5);
      real Prior_ob_9 = beta_rng(Prior_oe_9, Prior_oe_9);
      Prior_l_9_2_1 = ((2.0 * Prior_ob_9) + ((-1.0) * 1.0));
      Prior_l_9_2_2 = sqrt((1.0 + ((-1.0) * square(Prior_l_9_2_1))));
      real Prior_oy_9_2 = beta_rng(1.0, (Prior_oe_9 + ((-1.0) * 0.5)));
      real Prior_oz_9_2_1 = normal_rng(0.0, 1.0);
      real Prior_oz_9_2_2 = normal_rng(0.0, 1.0);
      real Prior_os_9_2 = (sqrt(Prior_oy_9_2) * inv(sqrt((square(Prior_oz_9_2_1) + square(Prior_oz_9_2_2)))));
      Prior_l_9_3_1 = (Prior_os_9_2 * Prior_oz_9_2_1);
      Prior_l_9_3_2 = (Prior_os_9_2 * Prior_oz_9_2_2);
      Prior_l_9_3_3 = sqrt((1.0 + ((-1.0) * Prior_oy_9_2)));
    }
    real Prior_a_p9_1_1 = Prior_m_2;
    real Prior_a_p9_1_2 = Prior_m_6;
    real Prior_a_p9_2_1 = Prior_m_3;
    real Prior_a_p9_2_2 = Prior_m_7;
    real Prior_a_p9_3_1 = Prior_m_4;
    real Prior_a_p9_3_2 = Prior_m_8;
    for (I in 1:(Mode == 1 ? J_grp : 0)) {
      real Prior_e_9_1 = normal_rng(0.0, 1.0);
      real Prior_e_9_2 = normal_rng(0.0, 1.0);
      real Prior_e_9_3 = normal_rng(0.0, 1.0);
      Prior_p_9_1[I] = (Prior_a_p9_1_1 + (Prior_a_p9_1_2 * Prior_e_9_1));
      Prior_p_9_2[I] = (Prior_a_p9_2_1 + (Prior_a_p9_2_2 * ((Prior_l_9_2_1 * Prior_e_9_1) + (Prior_l_9_2_2 * Prior_e_9_2))));
      Prior_p_9_3[I] = (Prior_a_p9_3_1 + (Prior_a_p9_3_2 * (((Prior_l_9_3_1 * Prior_e_9_1) + (Prior_l_9_3_2 * Prior_e_9_2)) + (Prior_l_9_3_3 * Prior_e_9_3))));
    }
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng((((a_g[g[Ppc_o1_rows]]) + ((b_g[g[Ppc_o1_rows]]) .* x[Ppc_o1_rows])) + ((c_g[g[Ppc_o1_rows]]) .* w[Ppc_o1_rows])), s);
    array[Prior_o1_n] real Prior_o1_draw = normal_rng((((Prior_p_9_1[g[Prior_o1_rows]]) + ((Prior_p_9_2[g[Prior_o1_rows]]) .* x[Prior_o1_rows])) + ((Prior_p_9_3[g[Prior_o1_rows]]) .* w[Prior_o1_rows])), Prior_m_5);
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
