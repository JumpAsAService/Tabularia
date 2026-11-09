package egress

import (
	"net/netip"
	"reflect"
	"testing"
)

func TestHostsOf(t *testing.T) {
	cases := []struct {
		endpoint, region, bucket string
		want                     []string
	}{
		{"https://s3.fr-par.scw.cloud", "fr-par", "scripts", []string{"s3.fr-par.scw.cloud:443", "scripts.s3.fr-par.scw.cloud:443"}},
		{"http://minio.example.com:9000/", "", "tabularia-scripts", []string{"minio.example.com:9000", "tabularia-scripts.minio.example.com:9000"}},
		{"HTTPS://Storage.GoogleAPIs.com.", "", "", []string{"storage.googleapis.com:443"}},
		{"http://minio.deps.svc.cluster.local:9000", "us-east-1", "b-1", []string{"b-1.minio.deps.svc.cluster.local:9000", "minio.deps.svc.cluster.local:9000"}},
		// an address: no virtual-hosted name beside it
		{"http://203.0.113.7:9000", "", "scripts", []string{"203.0.113.7:9000"}},
		{"http://[2001:db8::7]:9000", "", "scripts", []string{"[2001:db8::7]:9000"}},
		// a bucket that cannot be a name: the endpoint alone
		{"https://s3.example.com", "", "Not_A_Name", []string{"s3.example.com:443"}},
		// empty: AWS, in the region (us-east-1 without one)
		{"", "eu-west-1", "data", []string{"data.s3.amazonaws.com:443", "data.s3.dualstack.eu-west-1.amazonaws.com:443",
			"data.s3.eu-west-1.amazonaws.com:443", "s3.amazonaws.com:443", "s3.dualstack.eu-west-1.amazonaws.com:443", "s3.eu-west-1.amazonaws.com:443"}},
		{"", "", "", []string{"s3.amazonaws.com:443", "s3.dualstack.us-east-1.amazonaws.com:443", "s3.us-east-1.amazonaws.com:443"}},
		// not an endpoint
		{"ftp://files.example.com", "", "b", nil},
		{"s3.example.com", "", "b", nil},
		{"http://host:99999", "", "b", nil},
	}
	for _, c := range cases {
		if got := HostsOf(c.endpoint, c.region, c.bucket); !reflect.DeepEqual(got, c.want) {
			t.Errorf("HostsOf(%q, %q, %q) = %v, want %v", c.endpoint, c.region, c.bucket, got, c.want)
		}
	}
}

func TestIsClusterName(t *testing.T) {
	for host, want := range map[string]bool{
		"minio": true, "minio.deps.svc": true, "minio.deps.svc.cluster.local": true, "minio.deps.svc.cluster.local.": true,
		"minio.deps.svc.corp.internal": true, "s3.fr-par.scw.cloud": false, "minio.example.com": false, "10.0.0.5": false,
		"svc.example.com": false, "::1": false,
	} {
		if got := IsClusterName(host); got != want {
			t.Errorf("IsClusterName(%q) = %v", host, got)
		}
	}
}

func TestForbidden(t *testing.T) {
	for addr, want := range map[string]bool{
		"127.0.0.1": true, "127.9.9.9": true, "::1": true, "0.0.0.0": true, "::": true, "0.1.2.3": true,
		"169.254.169.254": true, "169.254.20.10": true, "fe80::1": true, "fd00:ec2::254": true, "100.100.100.200": true,
		"224.0.0.1": true, "ff02::1": true, "255.255.255.255": true, "::ffff:127.0.0.1": true, "::ffff:169.254.169.254": true,
		// the storage may well be on a private network: its name is the administrators' choice
		"10.0.0.5": false, "172.18.0.4": false, "192.168.1.10": false, "203.0.113.5": false, "2001:db8::5": false,
		"100.64.0.1": false,
	} {
		if got := Forbidden(netip.MustParseAddr(addr)); got != want {
			t.Errorf("Forbidden(%s) = %v", addr, got)
		}
	}
}
