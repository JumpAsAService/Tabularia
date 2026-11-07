package pod

import (
	"math"
	"math/big"

	"github.com/duckdb/duckdb-go/v2"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/clientip"
)

func clientIPFor(trusted []string) *clientip.Resolver { return clientip.New(trusted) }

func mkDecimal(v int64, scale uint8) duckdb.Decimal {
	return duckdb.Decimal{Width: 38, Scale: scale, Value: big.NewInt(v)}
}

func mkDecimalBig(v *big.Int, scale uint8) duckdb.Decimal {
	return duckdb.Decimal{Width: 38, Scale: scale, Value: v}
}

func nan() float64 { return math.NaN() }
