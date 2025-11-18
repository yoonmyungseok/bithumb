package org.study.privateKey.accounts;

import lombok.Builder;
import lombok.Getter;
import lombok.ToString;


/**
 * 1. ClassName     : AccountsDto
 * 2. FileName      : AccountsDto.java
 * 3. Package       : org.study.privateKey
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 16. 오후 8:11
 * 6. Modified Date : 25. 11. 16. 오후 8:11
 * 7. Comment       :
 */
@Getter
@ToString
@Builder
public class AccountsDto {
    
    private String korName; // 한글명
    private String engName; // 영문명
    private String balance; // 보유잔고
    private double avgPrice; // 평균매수가
    private int buyAmount; // 매수금액
    private int estimatedValue; // 평가금액
    private int unrealizedPnL; // 평가손익
    private double rateOfReturn; // 수익률 = (평가손익 / 매수금액) * 100
}
