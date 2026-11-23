data {
  int<lower=0> N;
  vector[N] score;
  vector[N] V_n2;
  int<lower=0, upper=1> Mode;
}
parameters {
  real V_n3;
  real V_n4;
  real<lower=0.0> V_n5;
}
model {
  vector[N] V_n6 = (V_n3 + (V_n4 * V_n2));
  V_n3 ~ normal(0.0, 10.0);
  V_n4 ~ normal(0.0, 1.0);
  V_n5 ~ exponential(1.0);
  score ~ normal(V_n6, V_n5);
}
