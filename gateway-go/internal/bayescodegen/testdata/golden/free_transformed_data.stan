data {
  int<lower=0> N;
  int<lower=1> J_grp;
  array[N] int<lower=1, upper=J_grp> g;
  vector[J_grp] h;
  vector[N] x;
  vector[N] y;
  int<lower=0, upper=1> Mode;
}
transformed data {
  real half = (log(2.0) / 2.0);
  vector[J_grp] log_area = log(h);
  vector[N] log_price = log(x);
  vector[N] log_price_sq = square(log_price);
}
parameters {
  real a;
  real b_area;
  real b_price;
  real b_curve;
  real<lower=0.0> s;
}
model {
  a ~ normal(0.0, 2.5);
  b_area ~ normal(0.0, 1.0);
  b_price ~ normal(0.0, 1.0);
  b_curve ~ normal(0.0, 1.0);
  s ~ exponential(1.0);
  y ~ normal((((a + (b_price * log_price)) + (b_curve * log_price_sq)) + ((b_area * half) * (log_area[g]))), s);
}
