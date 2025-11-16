package org.study.privateKey;

import lombok.Builder;
import lombok.Getter;
import lombok.ToString;

import java.math.BigDecimal;

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
    
    private String korName;
    private String engName;
    private String balance;
    private double avgPrice;
    private int buyAmount;
    private int estimatedValue;
}
