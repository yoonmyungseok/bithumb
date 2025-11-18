package org.study.publicKey.ticker;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.math.BigDecimal;

/**
 * 시세(Ticker) 정보 DTO
 */
@Data
@NoArgsConstructor
@AllArgsConstructor
@Builder
public class Ticker {

    // 종목 구분 코드
    private String market;

    // 최근 거래 일자 (UTC), yyyyMMdd
    private String trade_date;

    // 최근 거래 시각 (UTC), HHmmss
    private String trade_time;

    // 최근 거래 일자 (KST), yyyyMMdd
    private String trade_date_kst;

    // 최근 거래 시각 (KST), HHmmss
    private String trade_time_kst;

    // 최근 거래 일시 (UTC, Unix Timestamp)
    private Long trade_timestamp;

    // 시가
    private Double opening_price;

    // 고가
    private Double high_price;

    // 저가
    private Double low_price;

    // 종가(현재가)
    private BigDecimal trade_price;

    // 전일 종가(KST 0시 기준)
    private Double prev_closing_price;

    // EVEN / RISE / FALL
    private String change;

    // 변화액의 절대값
    private Double change_price;

    // 변화율의 절대값
    private Double change_rate;

    // 부호가 있는 변화액
    private Double signed_change_price;

    // 부호가 있는 변화율
    private Double signed_change_rate;

    // 가장 최근 거래량
    private Double trade_volume;

    // 누적 거래대금(KST 0시 기준)
    private Double acc_trade_price;

    // 24시간 누적 거래대금
    private Double acc_trade_price_24h;

    // 누적 거래량(KST 0시 기준)
    private Double acc_trade_volume;

    // 24시간 누적 거래량
    private Double acc_trade_volume_24h;

    // 52주 신고가
    private Double highest_52_week_price;

    // 52주 신고가 달성일, yyyy-MM-dd
    private String highest_52_week_date;

    // 52주 신저가
    private Double lowest_52_week_price;

    // 52주 신저가 달성일, yyyy-MM-dd
    private String lowest_52_week_date;

    // 타임스탬프
    private Long timestamp;
}