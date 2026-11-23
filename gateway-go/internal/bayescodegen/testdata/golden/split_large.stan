data {
  int<lower=0> N;
  int<lower=1> J_grp;
  array[N] int<lower=1, upper=J_grp> g;
  vector[J_grp] u;
  vector[N] x;
  vector[N] y;
  int<lower=0, upper=1> Mode;
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
