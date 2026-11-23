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
  real mu_a;
  real mu_b;
  real<lower=0.0> sigma;
  real<lower=0.0> tau_a;
  real<lower=0.0> tau_b;
  matrix[2, J_grp] Z_eff;
  cholesky_factor_corr[2] L_eff;
}
transformed parameters {
  vector[J_grp] a_g;
  vector[J_grp] b_g;
  {
    matrix[2, J_grp] W_eff = diag_pre_multiply(to_vector([tau_a, tau_b]), L_eff) * Z_eff;
    a_g = mu_a + to_vector(W_eff[1]);
    b_g = mu_b + to_vector(W_eff[2]);
  }
}
model {
  mu_a ~ normal(1200.0, (2.5 * 300.0));
  mu_b ~ normal(0.0, (2.5 * 300.0));
  sigma ~ exponential((1.0 / 300.0));
  tau_a ~ normal(0.0, 300.0);
  tau_b ~ normal(0.0, 300.0);
  to_vector(Z_eff) ~ std_normal();
  L_eff ~ lkj_corr_cholesky(2.0);
  y ~ normal(((a_g[g]) + ((b_g[g]) .* x)), sigma);
}
generated quantities {
  matrix[2, 2] rho_g = multiply_lower_tri_self_transpose(L_eff);
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
  real Prior_m_7_1;
  real Prior_m_7_2;
  real Prior_m_7_rho;
  vector[Mode == 1 ? J_grp : 0] Prior_p_7_1;
  vector[Mode == 1 ? J_grp : 0] Prior_p_7_2;
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
    real Prior_g_m2_2 = (2.5 * 300.0);
    Prior_m_2 = (normal_rng(1200.0, fmin(fmax(Prior_g_m2_2, 1e-300), 1e+300)) + (0.0 * Prior_g_m2_2));
    Prior_d_2 = Prior_m_2;
    real Prior_g_m3_2 = (2.5 * 300.0);
    Prior_m_3 = (normal_rng(0.0, fmin(fmax(Prior_g_m3_2, 1e-300), 1e+300)) + (0.0 * Prior_g_m3_2));
    Prior_d_3 = Prior_m_3;
    real Prior_g_m4_1 = (1.0 / 300.0);
    Prior_m_4 = (exponential_rng(fmin(fmax(Prior_g_m4_1, 1e-300), 1e+300)) + (0.0 * Prior_g_m4_1));
    Prior_d_4 = Prior_m_4;
    real Prior_u_m5 = (Phi(((0.0 + ((-1.0) * 0.0)) * inv(300.0))) + ((1.0 + ((-1.0) * Phi(((0.0 + ((-1.0) * 0.0)) * inv(300.0))))) * uniform_rng(0.0, 1.0)));
    Prior_m_5 = (0.0 + (300.0 * inv_Phi(fmin(fmax(Prior_u_m5, 0.0), 1.0))));
    Prior_d_5 = Prior_m_5;
    real Prior_u_m6 = (Phi(((0.0 + ((-1.0) * 0.0)) * inv(300.0))) + ((1.0 + ((-1.0) * Phi(((0.0 + ((-1.0) * 0.0)) * inv(300.0))))) * uniform_rng(0.0, 1.0)));
    Prior_m_6 = (0.0 + (300.0 * inv_Phi(fmin(fmax(Prior_u_m6, 0.0), 1.0))));
    Prior_d_6 = Prior_m_6;
    real Prior_g_m7_1_1 = Prior_m_2;
    real Prior_g_m7_1_2 = Prior_m_5;
    Prior_m_7_1 = (normal_rng(fmin(fmax(Prior_g_m7_1_1, (-1e+300)), 1e+300), fmin(fmax(Prior_g_m7_1_2, 1e-300), 1e+300)) + (0.0 * (Prior_g_m7_1_1 + Prior_g_m7_1_2)));
    real Prior_g_m7_2_1 = Prior_m_3;
    real Prior_g_m7_2_2 = Prior_m_6;
    Prior_m_7_2 = (normal_rng(fmin(fmax(Prior_g_m7_2_1, (-1e+300)), 1e+300), fmin(fmax(Prior_g_m7_2_2, 1e-300), 1e+300)) + (0.0 * (Prior_g_m7_2_1 + Prior_g_m7_2_2)));
    Prior_m_7_rho = ((2.0 * beta_rng(2.0, 2.0)) + ((-1.0) * 1.0));
    real Prior_l_7_2_1;
    real Prior_l_7_2_2;
    for (I in 1:(Mode == 1 ? 1 : 0)) {
      real Prior_oe_7 = 2.0;
      real Prior_ob_7 = beta_rng(Prior_oe_7, Prior_oe_7);
      Prior_l_7_2_1 = ((2.0 * Prior_ob_7) + ((-1.0) * 1.0));
      Prior_l_7_2_2 = sqrt((1.0 + ((-1.0) * square(Prior_l_7_2_1))));
    }
    real Prior_a_p7_1_1 = Prior_m_2;
    real Prior_a_p7_1_2 = Prior_m_5;
    real Prior_a_p7_2_1 = Prior_m_3;
    real Prior_a_p7_2_2 = Prior_m_6;
    for (I in 1:(Mode == 1 ? J_grp : 0)) {
      real Prior_e_7_1 = normal_rng(0.0, 1.0);
      real Prior_e_7_2 = normal_rng(0.0, 1.0);
      Prior_p_7_1[I] = (Prior_a_p7_1_1 + (Prior_a_p7_1_2 * Prior_e_7_1));
      Prior_p_7_2[I] = (Prior_a_p7_2_1 + (Prior_a_p7_2_2 * ((Prior_l_7_2_1 * Prior_e_7_1) + (Prior_l_7_2_2 * Prior_e_7_2))));
    }
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(((a_g[g[Ppc_o1_rows]]) + ((b_g[g[Ppc_o1_rows]]) .* x[Ppc_o1_rows])), sigma);
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(((Prior_p_7_1[g[Prior_o1_rows]]) + ((Prior_p_7_2[g[Prior_o1_rows]]) .* x[Prior_o1_rows])), Prior_m_4);
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
