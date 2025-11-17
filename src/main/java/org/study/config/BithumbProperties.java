package org.study.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Service;

/**
 * 1. ClassName     : BithumbProperties
 * 2. FileName      : BithumbProperties.java
 * 3. Package       : org.study
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 17. 오후 5:12
 * 6. Modified Date : 25. 11. 17. 오후 5:12
 * 7. Comment       :
 */
@Service
@ConfigurationProperties(prefix = "bithumb")
@Data
public class BithumbProperties {
    private String apiUrl;
    private String accessKey;
    private String secretKey;
}
