data {
  int<lower=0> N;
  int<lower=1> J_grp;
  array[N] int<lower=1, upper=J_grp> g;
  vector[J_grp] u;
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
  vector[N] xd;
  {
    vector[N] E_xd_1 = (((((((((x .* x) ./ (x + 1.0)) - ((x .* x) ./ (x + 1.0))) - ((x .* x) ./ (x + 1.0))) - ((x .* x) ./ (x + 1.0))) - ((x .* x) ./ (x + 1.0))) - ((x .* x) ./ (x + 1.0))) - ((x .* x) ./ (x + 1.0))) - ((x .* x) ./ (x + 1.0)));
    xd = (((((((E_xd_1 - ((x .* x) ./ (x + 1.0))) - ((x .* x) ./ (x + 1.0))) - ((x .* x) ./ (x + 1.0))) - ((x .* x) ./ (x + 1.0))) - ((x .* x) ./ (x + 1.0))) - ((x .* x) ./ (x + 1.0))) + 1.0);
  }
}
parameters {
  real a;
  real<lower=0.0> b;
  vector<lower=0.0>[J_grp] bg;
  real<lower=0.0> s;
  real<lower=0.0> tau;
  vector[J_grp] Z_al;
}
transformed parameters {
  vector[J_grp] al;
  {
    vector[J_grp] E_al_1 = (((((((((a * bg) ./ (bg + b)) - ((a * bg) ./ (bg + b))) - ((a * bg) ./ (bg + b))) - ((a * bg) ./ (bg + b))) - ((a * bg) ./ (bg + b))) - ((a * bg) ./ (bg + b))) - ((a * bg) ./ (bg + b))) - ((a * bg) ./ (bg + b)));
    al = ((((((E_al_1 - ((a * bg) ./ (bg + b))) - ((a * bg) ./ (bg + b))) - ((a * bg) ./ (bg + b))) - ((a * bg) ./ (bg + b))) + 1.0) + (tau * Z_al));
  }
  vector[J_grp] ug;
  {
    vector[J_grp] E_ug_1 = (((((((((a * u) ./ (u + b)) - ((a * u) ./ (u + b))) - ((a * u) ./ (u + b))) - ((a * u) ./ (u + b))) - ((a * u) ./ (u + b))) - ((a * u) ./ (u + b))) - ((a * u) ./ (u + b))) - ((a * u) ./ (u + b)));
    ug = (((((((E_ug_1 - ((a * u) ./ (u + b))) - ((a * u) ./ (u + b))) - ((a * u) ./ (u + b))) - ((a * u) ./ (u + b))) - ((a * u) ./ (u + b))) - ((a * u) ./ (u + b))) + 1.0);
  }
}
model {
  vector[N] E_m_1 = (((((((((a * x) ./ (x + b)) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b)));
  vector[N] E_m_2 = (((((((E_m_1 - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b)));
  vector[N] E_m_3 = (((((((E_m_2 - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b)));
  vector[N] E_m_4 = (((((((E_m_3 - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b)));
  vector[N] m = ((E_m_4 - ((a * x) ./ (x + b))) + 1.0);
  a ~ normal(0.0, 1.0);
  b ~ exponential(1.0);
  bg ~ exponential(1.0);
  s ~ exponential(1.0);
  tau ~ normal(0.0, 1.0);
  Z_al ~ std_normal();
  vector[N] E_lik_1 = (((((((((m + (al[g])) + (ug[g])) + ((xd .* x) ./ (x + b))) - ((xd .* x) ./ (x + b))) - ((xd .* x) ./ (x + b))) - ((xd .* x) ./ (x + b))) - ((xd .* x) ./ (x + b))) - ((xd .* x) ./ (x + b))) - ((xd .* x) ./ (x + b)));
  y ~ normal(((((((E_lik_1 - ((xd .* x) ./ (x + b))) - ((xd .* x) ./ (x + b))) - ((xd .* x) ./ (x + b))) - ((xd .* x) ./ (x + b))) - ((xd .* x) ./ (x + b))) + 1.0), s);
}
generated quantities {
  real Prior_m_1;
  real Prior_d_1;
  real Prior_m_2;
  real Prior_d_2;
  real Prior_m_3;
  vector[Mode == 1 ? J_grp : 0] Prior_p_3;
  real Prior_m_5;
  real Prior_d_5;
  real Prior_m_6;
  real Prior_d_6;
  real Prior_m_7;
  vector[Mode == 1 ? J_grp : 0] Prior_p_7;
  vector[Mode == 1 ? J_grp : 0] Prior_p_9;
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
    vector[N] E_m_1 = (((((((((a * x) ./ (x + b)) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b)));
    vector[N] E_m_2 = (((((((E_m_1 - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b)));
    vector[N] E_m_3 = (((((((E_m_2 - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b)));
    vector[N] E_m_4 = (((((((E_m_3 - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b))) - ((a * x) ./ (x + b)));
    vector[N] m = ((E_m_4 - ((a * x) ./ (x + b))) + 1.0);
    Prior_m_1 = normal_rng(0.0, 1.0);
    Prior_d_1 = Prior_m_1;
    Prior_m_2 = exponential_rng(1.0);
    Prior_d_2 = Prior_m_2;
    Prior_m_3 = exponential_rng(1.0);
    real Prior_a_p3_1 = 1.0;
    for (I in 1:(Mode == 1 ? J_grp : 0)) {
      Prior_p_3[I] = exponential_rng(Prior_a_p3_1);
    }
    Prior_m_5 = exponential_rng(1.0);
    Prior_d_5 = Prior_m_5;
    real Prior_u_m6 = (Phi(((0.0 + ((-1.0) * 0.0)) * inv(1.0))) + ((1.0 + ((-1.0) * Phi(((0.0 + ((-1.0) * 0.0)) * inv(1.0))))) * uniform_rng(0.0, 1.0)));
    Prior_m_6 = (0.0 + (1.0 * inv_Phi(fmin(fmax(Prior_u_m6, 0.0), 1.0))));
    Prior_d_6 = Prior_m_6;
    vector[J_grp] Prior_r_3;
    for (I in 1:J_grp) {
      Prior_r_3[I] = Prior_m_3;
    }
    vector[J_grp] E_al_2 = (((((((((Prior_m_1 * Prior_r_3) ./ (Prior_r_3 + Prior_m_2)) - ((Prior_m_1 * Prior_r_3) ./ (Prior_r_3 + Prior_m_2))) - ((Prior_m_1 * Prior_r_3) ./ (Prior_r_3 + Prior_m_2))) - ((Prior_m_1 * Prior_r_3) ./ (Prior_r_3 + Prior_m_2))) - ((Prior_m_1 * Prior_r_3) ./ (Prior_r_3 + Prior_m_2))) - ((Prior_m_1 * Prior_r_3) ./ (Prior_r_3 + Prior_m_2))) - ((Prior_m_1 * Prior_r_3) ./ (Prior_r_3 + Prior_m_2))) - ((Prior_m_1 * Prior_r_3) ./ (Prior_r_3 + Prior_m_2)));
    vector[J_grp] Prior_v_7_1 = (((((E_al_2 - ((Prior_m_1 * Prior_r_3) ./ (Prior_r_3 + Prior_m_2))) - ((Prior_m_1 * Prior_r_3) ./ (Prior_r_3 + Prior_m_2))) - ((Prior_m_1 * Prior_r_3) ./ (Prior_r_3 + Prior_m_2))) - ((Prior_m_1 * Prior_r_3) ./ (Prior_r_3 + Prior_m_2))) + 1.0);
    real Prior_g_m7_1 = Prior_v_7_1[1];
    real Prior_g_m7_2 = Prior_m_6;
    Prior_m_7 = (normal_rng(fmin(fmax(Prior_g_m7_1, (-1e+300)), 1e+300), fmin(fmax(Prior_g_m7_2, 1e-300), 1e+300)) + (0.0 * (Prior_g_m7_1 + Prior_g_m7_2)));
    vector[Mode == 1 ? J_grp : 0] Prior_a_p7_1;
    real Prior_a_p7_2 = Prior_m_6;
    for (I in 1:(Mode == 1 ? 1 : 0)) {
      vector[J_grp] E_al_3 = (((((((((Prior_m_1 * Prior_p_3) ./ (Prior_p_3 + Prior_m_2)) - ((Prior_m_1 * Prior_p_3) ./ (Prior_p_3 + Prior_m_2))) - ((Prior_m_1 * Prior_p_3) ./ (Prior_p_3 + Prior_m_2))) - ((Prior_m_1 * Prior_p_3) ./ (Prior_p_3 + Prior_m_2))) - ((Prior_m_1 * Prior_p_3) ./ (Prior_p_3 + Prior_m_2))) - ((Prior_m_1 * Prior_p_3) ./ (Prior_p_3 + Prior_m_2))) - ((Prior_m_1 * Prior_p_3) ./ (Prior_p_3 + Prior_m_2))) - ((Prior_m_1 * Prior_p_3) ./ (Prior_p_3 + Prior_m_2)));
      Prior_a_p7_1 = (((((E_al_3 - ((Prior_m_1 * Prior_p_3) ./ (Prior_p_3 + Prior_m_2))) - ((Prior_m_1 * Prior_p_3) ./ (Prior_p_3 + Prior_m_2))) - ((Prior_m_1 * Prior_p_3) ./ (Prior_p_3 + Prior_m_2))) - ((Prior_m_1 * Prior_p_3) ./ (Prior_p_3 + Prior_m_2))) + 1.0);
    }
    for (I in 1:(Mode == 1 ? J_grp : 0)) {
      Prior_p_7[I] = (normal_rng(fmin(fmax(Prior_a_p7_1[I], (-1e+300)), 1e+300), fmin(fmax(Prior_a_p7_2, 1e-300), 1e+300)) + (0.0 * (Prior_a_p7_1[I] + Prior_a_p7_2)));
    }
    for (I in 1:(Mode == 1 ? 1 : 0)) {
      vector[J_grp] E_ug_2 = (((((((((Prior_m_1 * u) ./ (u + Prior_m_2)) - ((Prior_m_1 * u) ./ (u + Prior_m_2))) - ((Prior_m_1 * u) ./ (u + Prior_m_2))) - ((Prior_m_1 * u) ./ (u + Prior_m_2))) - ((Prior_m_1 * u) ./ (u + Prior_m_2))) - ((Prior_m_1 * u) ./ (u + Prior_m_2))) - ((Prior_m_1 * u) ./ (u + Prior_m_2))) - ((Prior_m_1 * u) ./ (u + Prior_m_2)));
      Prior_p_9 = (((((((E_ug_2 - ((Prior_m_1 * u) ./ (u + Prior_m_2))) - ((Prior_m_1 * u) ./ (u + Prior_m_2))) - ((Prior_m_1 * u) ./ (u + Prior_m_2))) - ((Prior_m_1 * u) ./ (u + Prior_m_2))) - ((Prior_m_1 * u) ./ (u + Prior_m_2))) - ((Prior_m_1 * u) ./ (u + Prior_m_2))) + 1.0);
    }
    vector[N] E_lik_2;
    E_lik_2[Ppc_o1_rows] = (((((((((m[Ppc_o1_rows] + (al[g[Ppc_o1_rows]])) + (ug[g[Ppc_o1_rows]])) + ((xd[Ppc_o1_rows] .* x[Ppc_o1_rows]) ./ (x[Ppc_o1_rows] + b))) - ((xd[Ppc_o1_rows] .* x[Ppc_o1_rows]) ./ (x[Ppc_o1_rows] + b))) - ((xd[Ppc_o1_rows] .* x[Ppc_o1_rows]) ./ (x[Ppc_o1_rows] + b))) - ((xd[Ppc_o1_rows] .* x[Ppc_o1_rows]) ./ (x[Ppc_o1_rows] + b))) - ((xd[Ppc_o1_rows] .* x[Ppc_o1_rows]) ./ (x[Ppc_o1_rows] + b))) - ((xd[Ppc_o1_rows] .* x[Ppc_o1_rows]) ./ (x[Ppc_o1_rows] + b))) - ((xd[Ppc_o1_rows] .* x[Ppc_o1_rows]) ./ (x[Ppc_o1_rows] + b)));
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(((((((E_lik_2[Ppc_o1_rows] - ((xd[Ppc_o1_rows] .* x[Ppc_o1_rows]) ./ (x[Ppc_o1_rows] + b))) - ((xd[Ppc_o1_rows] .* x[Ppc_o1_rows]) ./ (x[Ppc_o1_rows] + b))) - ((xd[Ppc_o1_rows] .* x[Ppc_o1_rows]) ./ (x[Ppc_o1_rows] + b))) - ((xd[Ppc_o1_rows] .* x[Ppc_o1_rows]) ./ (x[Ppc_o1_rows] + b))) - ((xd[Ppc_o1_rows] .* x[Ppc_o1_rows]) ./ (x[Ppc_o1_rows] + b))) + 1.0), s);
    vector[N] E_m_5;
    E_m_5[Prior_o1_rows] = (((((((((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2)) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2)));
    vector[N] E_m_6;
    E_m_6[Prior_o1_rows] = (((((((E_m_5[Prior_o1_rows] - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2)));
    vector[N] E_m_7;
    E_m_7[Prior_o1_rows] = (((((((E_m_6[Prior_o1_rows] - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2)));
    vector[N] E_m_8;
    E_m_8[Prior_o1_rows] = (((((((E_m_7[Prior_o1_rows] - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2)));
    vector[N] E_m_9;
    E_m_9[Prior_o1_rows] = ((E_m_8[Prior_o1_rows] - ((Prior_m_1 * x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) + 1.0);
    vector[N] E_lik_3;
    E_lik_3[Prior_o1_rows] = (((((((((E_m_9[Prior_o1_rows] + (Prior_p_7[g[Prior_o1_rows]])) + (Prior_p_9[g[Prior_o1_rows]])) + ((xd[Prior_o1_rows] .* x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((xd[Prior_o1_rows] .* x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((xd[Prior_o1_rows] .* x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((xd[Prior_o1_rows] .* x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((xd[Prior_o1_rows] .* x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((xd[Prior_o1_rows] .* x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((xd[Prior_o1_rows] .* x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2)));
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(((((((E_lik_3[Prior_o1_rows] - ((xd[Prior_o1_rows] .* x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((xd[Prior_o1_rows] .* x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((xd[Prior_o1_rows] .* x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((xd[Prior_o1_rows] .* x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) - ((xd[Prior_o1_rows] .* x[Prior_o1_rows]) ./ (x[Prior_o1_rows] + Prior_m_2))) + 1.0), Prior_m_5);
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
