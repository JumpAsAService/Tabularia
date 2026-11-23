data {
  int<lower=0> N;
  int<lower=1> K_ch;
  array[N] int<lower=1, upper=K_ch> channel;
  vector[N] x;
  vector[N] y;
  int<lower=0, upper=1> Mode;
}
parameters {
  real alpha;
  vector[K_ch - 1] b_channel;
  real b_x;
  real<lower=0.0> sigma;
}
model {
  vector[K_ch] Full_bch = append_row(0.0, b_channel);
  vector[N] eta = ((alpha + (b_x * x)) + (Full_bch[channel]));
  alpha ~ normal(0.0, 2.5);
  b_channel ~ normal(0.0, 1.0);
  b_x ~ normal(0.0, 1.0);
  sigma ~ exponential(1.0);
  y ~ normal(eta, sigma);
}
