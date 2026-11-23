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
